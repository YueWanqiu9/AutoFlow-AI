"""服务人员资源池：资料、可用状态与查询，不处理工单或派单。"""

import json

from database import db
from modules import events

STATUS_LABELS = {"available": "可用", "busy": "忙碌", "inactive": "停用"}


def parse_skills(value: str | list[str] | None) -> list[str]:
    """统一解析 JSON 数组或列表，去除首尾空格并去重；空值返回空列表。"""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError) as error:
            raise ValueError("技能必须是 JSON 字符串数组或字符串列表。") from error
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError("技能必须是由非空文本组成的列表。")
    return list(dict.fromkeys(item.strip() for item in value))


def _text(value, label, *, required=False):
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"{label}必须是{'非空' if required else ''}文本。")
    return value.strip()


def _validate_status(status):
    status = _text(status, "人员状态", required=True)
    if status not in STATUS_LABELS:
        raise ValueError("人员状态必须是 available、busy 或 inactive。")
    return status


def _validate_id(staff_id):
    if type(staff_id) is not int or staff_id <= 0:
        raise ValueError("人员编号必须是正整数。")


def _staff_from_row(row) -> dict:
    result = dict(row)
    result["skills"] = parse_skills(result["skills"])
    result["is_demo"] = bool(result["is_demo"])
    return result


def _insert_staff(connection, name, phone="", skills=None, service_area="", *,
                  status="available", is_demo=False, actor_type="operator") -> dict:
    """普通创建和整批演示加载共用；由调用方管理事务。"""
    if not connection.in_transaction:
        raise ValueError("创建人员必须在业务事务中执行。")
    if type(is_demo) is not bool:
        raise ValueError("演示数据标记必须是布尔值。")
    values = (
        _text(name, "姓名", required=True), _text(phone, "电话"),
        json.dumps(parse_skills(skills), ensure_ascii=False), _text(service_area, "服务区域"),
        _validate_status(status), db._now_text(), int(is_demo),
    )
    cursor = connection.execute(
        """INSERT INTO staff (name, phone, skills, service_area, status, created_at, is_demo)
           VALUES (?, ?, ?, ?, ?, ?, ?)""", values,
    )
    staff = _staff_from_row(connection.execute("SELECT * FROM staff WHERE id = ?", (cursor.lastrowid,)).fetchone())
    events.create_staff_event(
        connection, staff["id"], "staff_created", actor_type, {}, staff,
        reason="加载虚构演示人员（demo），非真实员工。" if is_demo else "创建服务人员。",
    )
    return staff


def create_staff(name: str, phone: str = "", skills=None, service_area: str = "", *,
                 status: str = "available", is_demo: bool = False, actor_type: str = "operator") -> dict:
    """创建人员并记录事件，任一步失败全部回滚；返回解析后的人员字典。"""
    db.init_db()
    with db._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        return _insert_staff(connection, name, phone, skills, service_area,
                             status=status, is_demo=is_demo, actor_type=actor_type)


def list_staff(status: str | None = None, *, include_demo: bool = True) -> list[dict]:
    """按编号返回人员，可按状态筛选；include_demo=False 排除演示数据。"""
    if type(include_demo) is not bool:
        raise ValueError("是否包含演示数据必须是布尔值。")
    clauses, parameters = [], []
    if status is not None:
        clauses.append("status = ?")
        parameters.append(_validate_status(status))
    if not include_demo:
        clauses.append("is_demo = 0")
    query = "SELECT * FROM staff"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    db.init_db()
    with db._connection() as connection:
        rows = connection.execute(query + " ORDER BY id", parameters).fetchall()
    return [_staff_from_row(row) for row in rows]


def get_staff(staff_id: int) -> dict | None:
    """按稳定的人员编号查询，供后续关联使用；不存在时返回 None。"""
    _validate_id(staff_id)
    db.init_db()
    with db._connection() as connection:
        row = connection.execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()
    return _staff_from_row(row) if row is not None else None


def get_available_staff(*, include_demo: bool = True) -> list[dict]:
    """仅返回标记可用的人员；此状态不代表已验证档期或匹配某个工单。"""
    return list_staff("available", include_demo=include_demo)


def _update_staff(staff_id, fields, event_type, actor_type, reason):
    _validate_id(staff_id)
    reason = _text(reason, "操作原因")
    if actor_type not in ("system", "operator"):
        raise ValueError("操作来源必须是 system 或 operator。")
    db.init_db()
    with db._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()
        if row is None:
            return None
        if fields.get("status") == "available" and db.has_open_dispatch(connection, staff_id):
            raise ValueError("该人员仍有未结束派单，不能标记为可用。请先完成服务或处理相关派单。")
        before = _staff_from_row(row)
        after = {**before, **fields}
        changed = {key: value for key, value in fields.items() if before[key] != value}
        if not changed:
            return before
        # 字段名只来自下方两个公开函数的固定白名单，值统一参数绑定。
        values = [json.dumps(value, ensure_ascii=False) if key == "skills" else value
                  for key, value in changed.items()]
        connection.execute(
            "UPDATE staff SET " + ", ".join(f"{key} = ?" for key in changed) + " WHERE id = ?",
            (*values, staff_id),
        )
        events.create_staff_event(connection, staff_id, event_type, actor_type, before, after, reason)
        return after


def update_staff(staff_id: int, *, name=None, phone=None, skills=None, service_area=None,
                 reason: str = "", actor_type: str = "operator") -> dict | None:
    """局部更新资料；None 表示不修改，空文本/空列表可清空可选字段。"""
    fields = {}
    for key, value, label in (("name", name, "姓名"), ("phone", phone, "电话"), ("service_area", service_area, "服务区域")):
        if value is not None:
            fields[key] = _text(value, label, required=key == "name")
    if skills is not None:
        fields["skills"] = parse_skills(skills)
    return _update_staff(staff_id, fields, "staff_updated", actor_type, reason)


def update_staff_status(staff_id: int, status: str, *, reason: str = "", actor_type: str = "operator") -> dict | None:
    """人工维护资源状态并记录变化，不触发或修改任何工单。"""
    return _update_staff(staff_id, {"status": _validate_status(status)}, "staff_status_changed", actor_type, reason)


def get_staff_metrics() -> dict:
    """按当前资源状态统计，包含演示人员并单列演示数量。"""
    db.init_db()
    with db._connection() as connection:
        row = connection.execute(
            """SELECT COUNT(*) AS staff_total_count,
               COALESCE(SUM(status = 'available'), 0) AS staff_available_count,
               COALESCE(SUM(status = 'busy'), 0) AS staff_busy_count,
               COALESCE(SUM(is_demo = 1), 0) AS staff_demo_count FROM staff"""
        ).fetchone()
    return dict(row)
