"""运营确认派单：只建立关联和占用状态，不安排档期或启动服务。"""

from database import db
from modules import events
from modules.matching import match_staff
from modules.rules import COMPLETION_FIELDS, missing_ticket_fields
from modules.staff import _staff_from_row

FIELD_LABELS = {
    "customer_name": "客户姓名", "phone": "联系电话", "address": "服务地址",
    "service_type": "服务类型", "appointment_time": "预约时间",
}
STATUS_LABELS = {
    "confirmed": "已派单", "accepted": "已接单", "in_service": "服务中",
    "completed": "已完成", "exception": "异常", "cancelled": "已取消",
}
OPEN_STATUSES = db.OPEN_DISPATCH_STATUSES


def dispatch_snapshot(ticket: dict, person: dict) -> dict:
    """表单所确认的业务资料；保存前比较，防止沿用过期推荐。"""
    return {
        "ticket": {key: ticket.get(key) for key in (
            "id", *COMPLETION_FIELDS, "status", "review_required", "priority_source",
        )},
        "staff": {key: person.get(key) for key in (
            "id", "name", "skills", "service_area", "status", "is_demo",
        )},
    }


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}不能为空。")
    return value.strip()


def create_dispatch(ticket_id, staff_id, confirmed_by, reason, *,
                    busy_confirmed=False, expected_snapshot=None) -> int:
    """四参数接口默认拒绝忙碌人员；只有额外明确确认才允许。

    在取得写锁后重新校验资料、候选资格和重复派单，全部写入共用事务。
    expected_snapshot 可用于页面检测表单打开后资料变化；不保存推荐评分。
    """
    ticket_id = _required_text(ticket_id, "工单编号")
    confirmed_by = _required_text(confirmed_by, "确认人")
    reason = _required_text(reason, "派单原因")
    if type(staff_id) is not int or staff_id <= 0:
        raise ValueError("人员编号必须是正整数。")
    if type(busy_confirmed) is not bool:
        raise ValueError("忙碌人员确认必须是布尔值。")
    if expected_snapshot is not None and not isinstance(expected_snapshot, dict):
        raise ValueError("派单确认资料格式不正确。")
    db.init_db()
    with db._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            raise ValueError("工单不存在。")
        ticket = db._ticket_from_row(row)
        row = connection.execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()
        if row is None:
            raise ValueError("人员不存在。")
        person = _staff_from_row(row)
        if connection.execute(
            "SELECT id FROM dispatches WHERE ticket_id = ? AND status <> 'cancelled'", (ticket_id,),
        ).fetchone():
            raise ValueError("该工单已有未取消的派单（含已完成），不能重复派单。")
        if ticket["status"].strip() in ("已取消", "取消", "cancelled"):
            raise ValueError("工单已取消，禁止派单。")
        if ticket["status"].strip() == "已完成":
            raise ValueError("工单已完成，禁止派单。")
        missing = missing_ticket_fields(ticket)
        if missing:
            raise ValueError("工单信息不完整，请补充：" + "、".join(FIELD_LABELS[field] for field in missing) + "。")
        if ticket["review_required"] or ticket["status"].strip() == "人工审核":
            raise ValueError("工单尚需人工审核，请先完成审核。")
        if person["status"] == "inactive":
            raise ValueError("人员已停用，禁止派单。")
        if person["status"] == "busy" and not busy_confirmed:
            raise ValueError("人员当前忙碌，需要额外明确确认时间冲突后才能派单。")
        if not match_staff(ticket, staff_pool=[person]):
            raise ValueError("人员技能与当前工单不匹配，请重新查看推荐。")
        if expected_snapshot is not None and expected_snapshot != dispatch_snapshot(ticket, person):
            raise ValueError("工单或人员资料已变化，请关闭确认窗口并重新查看推荐。")

        now = db._now_text()
        is_demo = bool(person["is_demo"] or ticket_id.startswith("DEMO-") or ticket.get("priority_source") == "demo")
        cursor = connection.execute(
            """INSERT INTO dispatches
               (ticket_id, staff_id, status, assigned_at, confirmed_by, confirm_reason, created_at, is_demo)
               VALUES (?, ?, 'confirmed', ?, ?, ?, ?, ?)""",
            (ticket_id, staff_id, now, confirmed_by, reason, now, int(is_demo)),
        )
        dispatch_id = cursor.lastrowid
        connection.execute("UPDATE staff SET status = 'busy' WHERE id = ?", (staff_id,))
        events.create_staff_event(
            connection, staff_id, "staff_status_changed", "operator", person,
            {**person, "status": "busy"}, reason=f"派单 #{dispatch_id}；确认人：{confirmed_by}；{reason}",
        )
        after = {
            "dispatch_id": dispatch_id, "staff_id": staff_id, "dispatch_status": "confirmed",
            "confirmed_by": confirmed_by, "assigned_at": now, "is_demo": is_demo,
            "busy_confirmed": person["status"] == "busy" and busy_confirmed,
        }
        events.create_event(connection, ticket_id, "dispatch_created", "operator", {}, after, reason=reason)
        events.create_event(
            connection, ticket_id, "staff_assigned", "operator",
            {"staff_id": staff_id, "staff_status": person["status"]},
            {**after, "staff_status": "busy"}, reason=reason,
        )
        return dispatch_id


def get_ticket_dispatches(ticket_id: str) -> list[dict]:
    """返回派单及已记录服务进度；未发生的服务时点保持为空。"""
    ticket_id = _required_text(ticket_id, "工单编号")
    db.init_db()
    with db._connection() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM dispatches WHERE ticket_id = ? ORDER BY id DESC", (ticket_id,),
        )]


def get_dispatch_metrics() -> dict:
    """总数包含全部状态，confirmed 仅指等待接单；不由工单状态估算。"""
    db.init_db()
    with db._connection() as connection:
        row = connection.execute(
            """SELECT COUNT(*) AS dispatch_total_count,
                      COALESCE(SUM(status = 'confirmed'), 0) AS dispatch_confirmed_count,
                      COALESCE(SUM(status = 'cancelled'), 0) AS dispatch_cancelled_count,
                      COALESCE(SUM(is_demo = 1), 0) AS dispatch_demo_count
               FROM dispatches""",
        ).fetchone()
        return dict(row)
