"""运营后台登记服务进度；以派单为状态机，不覆盖工单的人工状态。"""

from datetime import datetime

from database import db
from modules import events
from modules.automation import REPORT_TIMEZONE
from modules.dispatch import STATUS_LABELS

# completed / cancelled 为终态；异常只能取消，不能跳过服务流程直接完成。
TRANSITIONS = {
    "confirmed": ("accepted", "cancelled"),
    "accepted": ("in_service", "exception", "cancelled"),
    "in_service": ("completed", "exception", "cancelled"),
    "exception": ("cancelled",),
    "completed": (), "cancelled": (),
}
EVENT_TYPES = {
    "accepted": "dispatch_accepted", "in_service": "service_started",
    "completed": "service_completed", "exception": "service_exception", "cancelled": "dispatch_cancelled",
}
TIME_FIELDS = {"accepted": "accepted_at", "in_service": "started_at", "completed": "completed_at"}


def _transition(dispatch_id, target, reason, *, note=None) -> dict:
    if type(dispatch_id) is not int or dispatch_id <= 0:
        raise ValueError("派单编号必须是正整数。")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("操作原因不能为空。")
    if note is not None and not isinstance(note, str):
        raise ValueError("完成备注必须是文本。")
    reason = reason.strip()
    db.init_db()
    with db._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM dispatches WHERE id = ?", (dispatch_id,)).fetchone()
        if row is None:
            raise ValueError("派单记录不存在，不能登记服务进度。")
        before = dict(row)
        if target not in TRANSITIONS.get(before["status"], ()):
            raise ValueError(f"非法状态流转：{STATUS_LABELS.get(before['status'], before['status'])}不能进入{STATUS_LABELS[target]}。请刷新查看最新进度。")
        person = connection.execute("SELECT * FROM staff WHERE id = ?", (before["staff_id"],)).fetchone()
        if person is None:
            raise ValueError("关联人员不存在，不能登记服务进度。")
        # 人员停用后允许记录已经发生的完成、异常或取消，但禁止继续接单／开始。
        if target in ("accepted", "in_service") and person["status"] == "inactive":
            raise ValueError("关联人员已停用，不能接单或开始服务。")
        fields = {"status": target}
        if target in TIME_FIELDS:
            fields[TIME_FIELDS[target]] = db._now_text()
        if target == "exception":
            fields["exception_reason"] = reason
        if target == "completed":
            fields["completed_note"] = (note or "").strip()
        assignments = ", ".join(f"{field} = ?" for field in fields)
        connection.execute(f"UPDATE dispatches SET {assignments} WHERE id = ?", (*fields.values(), dispatch_id))

        staff_status = person["status"]
        if target in ("accepted", "in_service", "exception") and staff_status == "available":
            # 旧版本曾允许人工误释放；只在明确登记进度时纠正，并与事件一起提交。
            staff_status = "busy"
        if target == "completed" and staff_status == "busy":
            if not db.has_open_dispatch(connection, person["id"]):
                staff_status = "available"
        if staff_status != person["status"]:
            connection.execute("UPDATE staff SET status = ? WHERE id = ?", (staff_status, person["id"]))
            events.create_staff_event(
                connection, person["id"], "staff_status_changed", "operator",
                {"status": person["status"]}, {"status": staff_status},
                reason=f"派单 #{dispatch_id} 登记{STATUS_LABELS[target]}，同步人员占用；{reason}",
            )

        def event_data(record, status):
            return {
                "dispatch_id": record["id"], "staff_id": record["staff_id"], "staff_status": status,
                "dispatch_status": record["status"],
                **{field: record[field] for field in (*TIME_FIELDS.values(), "exception_reason", "completed_note")},
            }

        after = {**before, **fields}
        events.create_event(
            connection, before["ticket_id"], EVENT_TYPES[target], "operator",
            event_data(before, person["status"]), event_data(after, staff_status), reason=reason,
        )
        return after


def accept_dispatch(dispatch_id) -> dict:
    return _transition(dispatch_id, "accepted", "运营确认人员接单。")


def start_service(dispatch_id) -> dict:
    return _transition(dispatch_id, "in_service", "运营登记开始服务。")


def complete_service(dispatch_id, note) -> dict:
    if not isinstance(note, str):
        raise ValueError("完成备注必须是文本。")
    return _transition(dispatch_id, "completed", note.strip() or "运营确认服务完成。", note=note)


def mark_exception(dispatch_id, reason) -> dict:
    return _transition(dispatch_id, "exception", reason)


def cancel_dispatch(dispatch_id, reason) -> dict:
    """取消未完成派单并记录原因；依本阶段规则，取消不自动释放人员。"""
    return _transition(dispatch_id, "cancelled", reason)


def _duration_seconds(start, end):
    """旧数据缺失／异常／负时长不参与平均；无时区的旧时间按北京时间。"""
    if not isinstance(start, str) or not isinstance(end, str) or not start.strip() or not end.strip():
        return None
    try:
        first, last = datetime.fromisoformat(start), datetime.fromisoformat(end)
        first = first if first.tzinfo else first.replace(tzinfo=REPORT_TIMEZONE)
        last = last if last.tzinfo else last.replace(tzinfo=REPORT_TIMEZONE)
        seconds = (last - first).total_seconds()
    except (ValueError, OverflowError):
        return None
    return seconds if seconds >= 0 else None


def get_service_metrics() -> dict:
    db.init_db()
    with db._connection() as connection:
        records = [dict(row) for row in connection.execute(
            "SELECT status, assigned_at, accepted_at, started_at, completed_at FROM dispatches"
        )]
    acceptance, service = [], []
    for record in records:
        elapsed = _duration_seconds(record["assigned_at"], record["accepted_at"])
        if elapsed is not None:
            acceptance.append(elapsed)
        if record["status"] == "completed":
            elapsed = _duration_seconds(record["started_at"], record["completed_at"])
            if elapsed is not None:
                service.append(elapsed)
    return {
        "service_completed_count": sum(record["status"] == "completed" for record in records),
        "service_exception_count": sum(record["status"] == "exception" for record in records),
        "avg_acceptance_seconds": sum(acceptance) / len(acceptance) if acceptance else None,
        "avg_service_seconds": sum(service) / len(service) if service else None,
    }
