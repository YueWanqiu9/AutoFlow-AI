"""工单、人员事件的创建与查询；写入必须使用业务操作的同一个事务。"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4


TRACKED_FIELDS = (
    "service_type", "appointment_time", "address", "customer_name",
    "phone", "urgency", "status", "review_required",
    "priority_suggested", "priority_final", "priority_reason", "priority_source",
    "advice_json", "advice_source", "advice_decision", "advice_final", "advice_generated_at",
)
CONVERSATION_FIELDS = ("message_id", "question_field", "missing_fields", "completion_status", "source")
DISPATCH_FIELDS = (
    "dispatch_id", "staff_id", "staff_status", "dispatch_status", "confirmed_by",
    "assigned_at", "is_demo", "busy_confirmed",
    "accepted_at", "started_at", "completed_at", "exception_reason", "completed_note",
)
SERVICE_EVENTS = ("dispatch_accepted", "service_started", "service_completed", "service_exception", "dispatch_cancelled")


def create_event(
    connection: sqlite3.Connection,
    ticket_id: str,
    event_type: str,
    actor_type: str,
    before_data: dict,
    after_data: dict,
    reason: str = "",
    operation_id: str | None = None,
) -> int | None:
    """仅保存实际变化的业务字段；不自行开启、提交或吞掉事务异常。"""
    if not connection.in_transaction:
        raise ValueError("事件必须在工单业务事务中写入。")
    for value in (ticket_id, event_type, actor_type):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("事件的工单编号、类型和操作来源不能为空。")
    if operation_id is None:
        operation_id = str(uuid4())
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise ValueError("操作编号不能为空。")

    fields = TRACKED_FIELDS
    if event_type.startswith("conversation_") or event_type == "field_filled":
        fields += CONVERSATION_FIELDS
    if event_type in ("dispatch_created", "staff_assigned", *SERVICE_EVENTS):
        fields += DISPATCH_FIELDS
    changed = [
        field for field in fields
        if before_data.get(field) != after_data.get(field)
    ]
    if not changed:
        return None
    before = {field: before_data[field] for field in changed if field in before_data}
    after = {field: after_data[field] for field in changed if field in after_data}
    if event_type == "advice_reviewed":
        # 即使建议本身未变化，评审事件也保留当时的依据与生成时间。
        for field in ("advice_json", "advice_source", "advice_generated_at"):
            before[field] = before_data.get(field)
            after[field] = after_data.get(field)
    if event_type == "staff_assigned":
        # 忙碌人员经明确确认后仍为 busy，也保留人员及状态依据。
        for field in ("staff_id", "staff_status", "confirmed_by", "busy_confirmed"):
            before[field] = before_data.get(field)
            after[field] = after_data.get(field)
    if event_type in SERVICE_EVENTS:
        # 工单可能有已取消的历史派单；事件必须能定位具体派单和人员。
        for field in ("dispatch_id", "staff_id", "staff_status"):
            before[field] = before_data.get(field)
            after[field] = after_data.get(field)
    cursor = connection.execute(
        """INSERT INTO ticket_events (
            ticket_id, operation_id, event_type, actor_type,
            before_data, after_data, reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            ticket_id, operation_id, event_type, actor_type,
            json.dumps(before, ensure_ascii=False, allow_nan=False),
            json.dumps(after, ensure_ascii=False, allow_nan=False),
            reason,
            datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="microseconds"),
        ),
    )
    return cursor.lastrowid


def get_ticket_events(ticket_id: str) -> list[dict]:
    """按时间和编号正序返回事件，前后数据解析为字典；不补造历史。"""
    # 延迟导入，避免数据库写入路径与事件查询之间的循环导入。
    from database import db

    db.init_db()
    with db._connection() as connection:
        rows = connection.execute(
            """SELECT * FROM ticket_events WHERE ticket_id = ?
               ORDER BY created_at ASC, id ASC""",
            (ticket_id,),
        ).fetchall()
    events = []
    for row in rows:
        event = dict(row)
        for field in ("before_data", "after_data"):
            event[field] = json.loads(event[field])
            if not isinstance(event[field], dict):
                raise ValueError("工单事件的变化内容格式错误。")
        events.append(event)
    return events


def create_staff_event(
    connection: sqlite3.Connection,
    staff_id: int,
    event_type: str,
    actor_type: str,
    before_data: dict,
    after_data: dict,
    reason: str = "",
) -> int | None:
    """人员事件单独关联 staff，不借用工单编号；由调用方提交或回滚。"""
    if not connection.in_transaction:
        raise ValueError("人员事件必须在人员业务事务中写入。")
    if type(staff_id) is not int or staff_id <= 0:
        raise ValueError("人员编号必须是正整数。")
    if event_type not in ("staff_created", "staff_updated", "staff_status_changed"):
        raise ValueError("人员事件类型不正确。")
    if actor_type not in ("system", "operator"):
        raise ValueError("操作来源必须是 system 或 operator。")
    if not isinstance(reason, str):
        raise ValueError("操作原因必须是文本。")
    fields = ("name", "phone", "skills", "service_area", "status", "created_at", "is_demo")
    changed = [field for field in fields if before_data.get(field) != after_data.get(field)]
    if not changed:
        return None
    before = {field: before_data[field] for field in changed if field in before_data}
    after = {field: after_data[field] for field in changed if field in after_data}
    cursor = connection.execute(
        """INSERT INTO staff_events (
            staff_id, operation_id, event_type, actor_type,
            before_data, after_data, reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (staff_id, str(uuid4()), event_type, actor_type,
         json.dumps(before, ensure_ascii=False, allow_nan=False),
         json.dumps(after, ensure_ascii=False, allow_nan=False), reason.strip(),
         datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="microseconds")),
    )
    return cursor.lastrowid


def get_staff_events(staff_id: int) -> list[dict]:
    """查询人员操作历史，按时间和编号正序返回；JSON 变化内容为字典。"""
    from database import db

    if type(staff_id) is not int or staff_id <= 0:
        raise ValueError("人员编号必须是正整数。")
    db.init_db()
    with db._connection() as connection:
        rows = connection.execute(
            "SELECT * FROM staff_events WHERE staff_id = ? ORDER BY created_at, id",
            (staff_id,),
        ).fetchall()
    result = []
    for row in rows:
        event = dict(row)
        for field in ("before_data", "after_data"):
            event[field] = json.loads(event[field])
            if not isinstance(event[field], dict):
                raise ValueError("人员事件的变化内容格式错误。")
        result.append(event)
    return result
