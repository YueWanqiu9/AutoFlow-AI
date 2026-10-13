"""SQLite 工单及服务人员存储，每次操作使用独立连接并在结束时关闭。"""

import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules import events
from modules.rules import COMPLETION_FIELDS, missing_ticket_fields

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "autoflow.db"
PRIORITY_FIELDS = ("priority_suggested", "priority_final", "priority_reason", "priority_source")
ADVICE_FIELDS = ("advice_json", "advice_source", "advice_decision", "advice_final", "advice_generated_at")
OPEN_DISPATCH_STATUSES = ("confirmed", "accepted", "in_service", "exception")


def has_open_dispatch(connection, staff_id: int) -> bool:
    """在调用方当前事务内检查人员占用，避免维护与服务完成使用不同规则。"""
    placeholders = ",".join("?" for _ in OPEN_DISPATCH_STATUSES)
    return connection.execute(
        f"SELECT 1 FROM dispatches WHERE staff_id = ? AND status IN ({placeholders}) LIMIT 1",
        (staff_id, *OPEN_DISPATCH_STATUSES),
    ).fetchone() is not None


@contextmanager
def _connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            yield connection
    finally:
        connection.close()


def init_db() -> None:
    """创建数据库和工单表；重复调用不会清空已有数据。"""
    with _connection() as connection:
        # 让并发初始化依次检查和迁移，避免重复添加同一列。
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id TEXT PRIMARY KEY NOT NULL,
                customer_input TEXT NOT NULL,
                service_type TEXT NOT NULL DEFAULT '',
                appointment_time TEXT NOT NULL DEFAULT '',
                address TEXT NOT NULL DEFAULT '',
                customer_name TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                urgency TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                created_time TEXT NOT NULL,
                confidence_score TEXT NOT NULL DEFAULT '{}',
                review_required TEXT NOT NULL DEFAULT 'false',
                token_used INTEGER NOT NULL DEFAULT 0,
                response_time REAL DEFAULT NULL
            )
            """
        )
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(tickets)")
        }
        if "confidence_score" not in columns:
            connection.execute(
                "ALTER TABLE tickets ADD COLUMN confidence_score TEXT NOT NULL DEFAULT '{}'"
            )
        if "review_required" not in columns:
            connection.execute(
                "ALTER TABLE tickets ADD COLUMN review_required TEXT NOT NULL DEFAULT 'false'"
            )
        if "token_used" not in columns:
            connection.execute(
                "ALTER TABLE tickets ADD COLUMN token_used INTEGER NOT NULL DEFAULT 0"
            )
        if "response_time" not in columns:
            connection.execute(
                "ALTER TABLE tickets ADD COLUMN response_time REAL DEFAULT NULL"
            )
        for field in PRIORITY_FIELDS:
            if field not in columns:
                connection.execute(f"ALTER TABLE tickets ADD COLUMN {field} TEXT DEFAULT NULL")
        for field in ADVICE_FIELDS:
            if field not in columns:
                if field == "advice_decision":
                    connection.execute("ALTER TABLE tickets ADD COLUMN advice_decision TEXT DEFAULT 'unreviewed'")
                    # 只在首次增加该列时清空旧行；以后插入的新工单使用默认值。
                    connection.execute("UPDATE tickets SET advice_decision = NULL")
                else:
                    connection.execute(f"ALTER TABLE tickets ADD COLUMN {field} TEXT DEFAULT NULL")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS ticket_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id TEXT NOT NULL REFERENCES tickets(id),
                operation_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                actor_type TEXT NOT NULL,
                before_data TEXT NOT NULL DEFAULT '{}',
                after_data TEXT NOT NULL DEFAULT '{}',
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS idx_ticket_events_history
               ON ticket_events (ticket_id, created_at, id)"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS conversation_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id TEXT NOT NULL REFERENCES tickets(id),
                role TEXT NOT NULL CHECK (role IN ('system', 'assistant', 'user')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS idx_conversation_history
               ON conversation_messages (ticket_id, id)"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS ticket_missing_fields (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id TEXT NOT NULL REFERENCES tickets(id),
                field_name TEXT NOT NULL CHECK (field_name IN
                    ('customer_name', 'phone', 'address', 'service_type', 'appointment_time')),
                status TEXT NOT NULL CHECK (status IN ('missing', 'filled', 'ignored')),
                created_at TEXT NOT NULL,
                filled_at TEXT,
                UNIQUE (ticket_id, field_name)
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS staff (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL CHECK (length(trim(name)) > 0),
                phone TEXT,
                skills TEXT NOT NULL DEFAULT '[]',
                service_area TEXT,
                status TEXT NOT NULL DEFAULT 'available'
                    CHECK (status IN ('available', 'busy', 'inactive')),
                created_at TEXT NOT NULL,
                is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1))
            )"""
        )
        # 演示标记独立保存，不依赖姓名判断；不补造人员或人员历史。
        staff_columns = {row["name"] for row in connection.execute("PRAGMA table_info(staff)")}
        if "is_demo" not in staff_columns:
            connection.execute("ALTER TABLE staff ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1))")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_staff_status ON staff (status)")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS staff_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                staff_id INTEGER NOT NULL REFERENCES staff(id),
                operation_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL CHECK (event_type IN
                    ('staff_created', 'staff_updated', 'staff_status_changed')),
                actor_type TEXT NOT NULL CHECK (actor_type IN ('system', 'operator')),
                before_data TEXT NOT NULL DEFAULT '{}',
                after_data TEXT NOT NULL DEFAULT '{}',
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS idx_staff_events_history
               ON staff_events (staff_id, created_at, id)"""
        )
        _migrate_dispatches(connection)


def _migrate_dispatches(connection):
    """SQLite 不能直接扩展 CHECK；在当前事务中复制原行、替换旧表。

    只改变派单状态约束与新增列，保留编号、原字段、用户索引和触发器。
    新时间字段保持 NULL，不补造服务历史。迁移失败由外层完整回滚。
    """
    schema = connection.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'dispatches'").fetchone()
    rebuild = schema is not None and "'in_service'" not in schema["sql"]
    table = "dispatches_migrating" if rebuild else "dispatches"
    connection.execute(
            f"""CREATE TABLE IF NOT EXISTS {table} (
                id INTEGER PRIMARY KEY,
                ticket_id TEXT NOT NULL REFERENCES tickets(id),
                staff_id INTEGER NOT NULL REFERENCES staff(id),
                status TEXT NOT NULL CHECK (status IN
                    ('confirmed', 'accepted', 'in_service', 'completed', 'exception', 'cancelled')),
                assigned_at TEXT,
                confirmed_by TEXT,
                confirm_reason TEXT,
                created_at TEXT,
                is_demo INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1)),
                accepted_at TEXT,
                started_at TEXT,
                completed_at TEXT,
                exception_reason TEXT,
                completed_note TEXT
            )"""
    )
    if rebuild:
        retained_objects = connection.execute(
            """SELECT sql FROM sqlite_master WHERE tbl_name = 'dispatches'
               AND type IN ('index', 'trigger') AND sql IS NOT NULL
               AND name NOT IN ('idx_dispatches_one_confirmed', 'idx_dispatches_one_effective')"""
        ).fetchall()
        old_columns = {row["name"] for row in connection.execute("PRAGMA table_info(dispatches)")}
        columns = [row["name"] for row in connection.execute(f"PRAGMA table_info({table})") if row["name"] in old_columns]
        names = ", ".join(columns)
        connection.execute(f"INSERT INTO {table} ({names}) SELECT {names} FROM dispatches")
        connection.execute("DROP TABLE dispatches")
        connection.execute(f"ALTER TABLE {table} RENAME TO dispatches")
        for item in retained_objects:
            connection.execute(item["sql"])
    # 支持已经扩展状态约束、但尚未添加时间列的数据库。
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(dispatches)")}
    for field in ("accepted_at", "started_at", "completed_at", "exception_reason", "completed_note"):
        if field not in columns:
            connection.execute(f"ALTER TABLE dispatches ADD COLUMN {field} TEXT")
    connection.execute("DROP INDEX IF EXISTS idx_dispatches_one_confirmed")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_dispatches_ticket ON dispatches (ticket_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_dispatches_staff ON dispatches (staff_id)")
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_dispatches_one_effective
           ON dispatches (ticket_id) WHERE status <> 'cancelled'"""
    )


def save_ticket(ticket: dict) -> None:
    """在同一事务内保存工单与创建事件；响应耗时单位为秒。"""
    token_used = ticket.get("token_used", 0)
    response_time = ticket.get("response_time")
    if isinstance(token_used, bool) or not isinstance(token_used, int) or token_used < 0:
        raise ValueError("Token 用量必须是非负整数。")
    if response_time is not None:
        if (
            isinstance(response_time, bool)
            or not isinstance(response_time, (int, float))
            or not math.isfinite(response_time)
            or response_time < 0
        ):
            raise ValueError("响应耗时必须是非负有限数值或为空。")
        response_time = float(response_time)

    init_db()
    with _connection() as connection:
        connection.execute(
            """
            INSERT INTO tickets (
                id, customer_input, service_type, appointment_time, address,
                customer_name, phone, urgency, status, created_time,
                confidence_score, review_required, token_used, response_time,
                priority_suggested, priority_final, priority_reason, priority_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticket["id"],
                ticket["customer_input"],
                ticket.get("service_type", ""),
                ticket.get("appointment_time", ""),
                ticket.get("address", ""),
                ticket.get("customer_name", ""),
                ticket.get("phone", ""),
                ticket.get("urgency", ""),
                ticket["status"],
                ticket["created_time"],
                ticket.get("confidence_score", "{}"),
                "true" if ticket.get("review_required", False) else "false",
                token_used,
                response_time,
                *(ticket.get(field) for field in PRIORITY_FIELDS),
            ),
        )
        saved = connection.execute("SELECT * FROM tickets WHERE id = ?", (ticket["id"],)).fetchone()
        events.create_event(
            connection, ticket["id"], "ticket_created", "system",
            {}, _ticket_from_row(saved),
            reason="创建工单并保存初始状态。",
        )


def _ticket_from_row(row: sqlite3.Row) -> dict:
    ticket = dict(row)
    ticket["review_required"] = ticket["review_required"] == "true"
    return ticket


def get_all_tickets() -> list[dict]:
    """查询全部工单，按创建时间从新到旧返回字典列表。"""
    init_db()
    with _connection() as connection:
        rows = connection.execute(
            "SELECT * FROM tickets ORDER BY created_time DESC, rowid DESC"
        ).fetchall()
        return [_ticket_from_row(row) for row in rows]


def get_ticket(ticket_id: str) -> dict | None:
    """按编号查询工单；不存在时返回 None。"""
    init_db()
    with _connection() as connection:
        row = connection.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        return _ticket_from_row(row) if row is not None else None


def update_ticket_after_review(ticket_id: str, fields: dict, status: str) -> bool:
    """保存已验证的人工修正，解除审核标记；保留原始 AI 结果和创建时间。"""
    init_db()
    with _connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None or row["review_required"] != "true":
            return False
        before = _ticket_from_row(row)
        cursor = connection.execute(
            """
            UPDATE tickets
            SET service_type = ?, appointment_time = ?, address = ?,
                customer_name = ?, phone = ?, urgency = ?, status = ?,
                review_required = 'false'
            WHERE id = ? AND review_required = 'true'
            """,
            (
                fields["service_type"],
                fields["appointment_time"],
                fields["address"],
                fields["customer_name"],
                fields["phone"],
                fields["urgency"],
                status,
                ticket_id,
            ),
        )
        events.create_event(
            connection, ticket_id, "ticket_reviewed", "operator",
            before, {**before, **fields, "status": status, "review_required": False},
            reason="运营人员核对字段并完成人工审核。",
        )
        _sync_after_manual_review(connection, before, {**before, **fields, "status": status, "review_required": False})
        return cursor.rowcount > 0


def update_ticket_status(ticket_id: str, status: str) -> bool:
    """更新普通工单状态；审核中的工单必须先完成人工审核。"""
    if not isinstance(status, str) or not status.strip():
        raise ValueError("工单状态不能为空。")
    init_db()
    with _connection() as connection:
        # 读取标记前获取写锁，避免检查后被其他连接标记为需要审核。
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if row is None:
            return False
        if row["review_required"] == "true":
            raise ValueError("该工单需要先完成人工审核。")

        status = status.strip()
        if status in ("待派单", "服务中") and not row["address"].strip():
            raise ValueError("缺少服务地址，不能进入待派单或服务中。")
        if row["status"] == status:
            return True
        before = _ticket_from_row(row)
        cursor = connection.execute(
            "UPDATE tickets SET status = ?, review_required = ? WHERE id = ?",
            (status, "true" if status == "人工审核" else "false", ticket_id),
        )
        events.create_event(
            connection, ticket_id, "status_changed", "operator",
            before, {**before, "status": status, "review_required": status == "人工审核"},
            reason="运营人员修改工单状态。",
        )
        return cursor.rowcount > 0


def save_priority_suggestion(ticket_id: str, suggestion: dict, expected_ticket: dict) -> dict | None:
    """保存建议与事件；不更改最终等级，拒绝覆盖调用期间已变化的工单。"""
    if (set(suggestion) != {"level", "reason", "source"}
            or suggestion["level"] not in ("高", "普通", "低", "待人工分级")
            or suggestion["source"] not in ("ai", "rule", "demo")
            or not isinstance(suggestion["reason"], str) or not suggestion["reason"].strip()):
        raise ValueError("优先级建议格式错误。")
    init_db()
    with _connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            return None
        before = _ticket_from_row(row)
        if before != expected_ticket:
            raise ValueError("工单已被修改，请重新生成优先级建议。")
        changes = {
            "priority_suggested": suggestion["level"],
            "priority_reason": suggestion["reason"].strip(),
            "priority_source": suggestion["source"],
        }
        connection.execute(
            """UPDATE tickets SET priority_suggested = ?, priority_reason = ?, priority_source = ?
               WHERE id = ?""",
            (changes["priority_suggested"], changes["priority_reason"], changes["priority_source"], ticket_id),
        )
        events.create_event(
            connection, ticket_id, "priority_generated", "system",
            {field: before[field] for field in changes}, changes,
            reason="按规则生成优先级建议；最终等级由运营人员确认。",
        )
        return {**before, **changes}


def confirm_ticket_priority(ticket_id: str, priority: str, reason: str) -> dict | None:
    """人工最终等级与确认事件原子保存，审核状态保持独立。"""
    if priority not in ("高", "普通", "低"):
        raise ValueError("最终优先级只能选择高、普通或低。")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("确认或修改优先级必须填写原因。")
    init_db()
    with _connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            return None
        before = _ticket_from_row(row)
        if before["priority_final"] == priority:
            return before
        connection.execute("UPDATE tickets SET priority_final = ? WHERE id = ?", (priority, ticket_id))
        events.create_event(
            connection, ticket_id, "priority_confirmed", "operator",
            {"priority_final": before["priority_final"]}, {"priority_final": priority}, reason=reason.strip(),
        )
        return {**before, "priority_final": priority}


def save_advice_suggestion(ticket_id: str, advice: dict, expected_ticket: dict) -> dict | None:
    """建议与事件原子保存；网络请求期间不持有事务，落库前检查版本。"""
    from modules.advisor import validate_advice

    advice = validate_advice(advice)
    init_db()
    with _connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            return None
        before = _ticket_from_row(row)
        if before != expected_ticket:
            raise ValueError("工单已被修改，请刷新后重新生成处理建议。")
        if before["advice_decision"] not in (None, "unreviewed"):
            raise ValueError("已有人工评审结果，请直接修改最终方案，不能重新生成覆盖。")
        changes = {
            "advice_json": json.dumps(advice, ensure_ascii=False, allow_nan=False),
            "advice_source": advice["source"], "advice_decision": "unreviewed", "advice_final": None,
            "advice_generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="microseconds"),
        }
        connection.execute(
            """UPDATE tickets SET advice_json = ?, advice_source = ?, advice_decision = ?,
               advice_final = ?, advice_generated_at = ? WHERE id = ?""",
            (*changes.values(), ticket_id),
        )
        events.create_event(
            connection, ticket_id, "advice_generated", "system",
            {field: before[field] for field in ADVICE_FIELDS}, changes,
            reason="生成处理建议，等待运营评审；未改变工单状态。",
        )
        return {**before, **changes}


def confirm_ticket_advice(
    ticket_id: str, decision: str, final_advice: str, reason: str, *, expected_ticket: dict | None = None,
) -> dict | None:
    """人工评审和事件同事务；保留原始建议，拒绝时不保存采用方案。"""
    from modules.advisor import format_advice, validate_advice

    if decision not in ("accepted", "modified", "rejected"):
        raise ValueError("请选择采纳、修改后采纳或拒绝。")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("评审处理建议必须填写原因。")
    if not isinstance(final_advice, str):
        raise ValueError("最终处理方案必须为文本。")
    final_advice = final_advice.strip()
    if decision != "rejected" and not final_advice:
        raise ValueError("采纳或修改后采纳必须填写最终处理方案。")
    if decision == "rejected" and final_advice:
        raise ValueError("拒绝建议时不能同时保存采用方案。")
    init_db()
    with _connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            return None
        before = _ticket_from_row(row)
        if expected_ticket is not None and before != expected_ticket:
            raise ValueError("工单已被修改，请刷新后重新核对处理建议。")
        if not before["advice_json"]:
            raise ValueError("请先生成处理建议，再进行评审。")
        try:
            advice = validate_advice(json.loads(before["advice_json"]))
        except (ValueError, TypeError) as error:
            raise ValueError("已保存的处理建议格式错误，无法评审。") from error
        suggested_text = format_advice(advice)
        if decision == "accepted" and final_advice != suggested_text:
            raise ValueError("采纳必须保留完整原建议；调整内容请使用修改后采纳。")
        if decision == "modified" and final_advice == suggested_text:
            raise ValueError("方案未修改，请使用采纳建议。")
        final = None if decision == "rejected" else final_advice
        if before["advice_decision"] == decision and before["advice_final"] == final:
            return before
        changes = {"advice_decision": decision, "advice_final": final}
        connection.execute(
            "UPDATE tickets SET advice_decision = ?, advice_final = ? WHERE id = ?",
            (decision, final, ticket_id),
        )
        # 事件模块保留原建议快照及评审前后方案，以便追溯采纳的依据。
        events.create_event(
            connection, ticket_id, "advice_reviewed", "operator",
            {field: before[field] for field in ADVICE_FIELDS},
            {field: changes.get(field, before[field]) for field in ADVICE_FIELDS}, reason=reason.strip(),
        )
        return {**before, **changes}


def _now_text() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="microseconds")


def _manual_empty_fields(connection, ticket_id: str) -> set[str]:
    """保护人工明确清空过的字段；非空字段无论来源均不由补全覆盖。"""
    rows = connection.execute(
        "SELECT after_data FROM ticket_events WHERE ticket_id = ? AND event_type = 'ticket_reviewed' AND actor_type = 'operator'",
        (ticket_id,),
    )
    protected = set()
    for row in rows:
        values = json.loads(row["after_data"])
        for field in COMPLETION_FIELDS:
            if field in values and not (values[field] or "").strip():
                protected.add(field)
    return protected


def get_conversation_state(ticket_id: str) -> dict:
    """只查询当前工单的对话和跟踪行，不自动开始或补造历史。"""
    init_db()
    with _connection() as connection:
        messages = [dict(row) for row in connection.execute(
            "SELECT * FROM conversation_messages WHERE ticket_id = ? ORDER BY id", (ticket_id,),
        )]
        fields = [dict(row) for row in connection.execute(
            "SELECT * FROM ticket_missing_fields WHERE ticket_id = ? ORDER BY id", (ticket_id,),
        )]
        question = connection.execute(
            "SELECT after_data FROM ticket_events WHERE ticket_id = ? AND event_type = 'conversation_question_generated' ORDER BY id DESC LIMIT 1",
            (ticket_id,),
        ).fetchone()
        return {"messages": messages, "missing_fields": fields,
                "last_message_id": messages[-1]["id"] if messages else 0,
                "question_field": json.loads(question["after_data"]).get("question_field") if question else None,
                "protected_fields": sorted(_manual_empty_fields(connection, ticket_id))}


def _add_message(connection, ticket_id, role, content) -> int:
    return connection.execute(
        "INSERT INTO conversation_messages (ticket_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (ticket_id, role, content, _now_text()),
    ).lastrowid


def _sync_missing_fields(connection, ticket: dict) -> None:
    missing = missing_ticket_fields(ticket)
    protected = _manual_empty_fields(connection, ticket["id"])
    existing = {row["field_name"]: dict(row) for row in connection.execute(
        "SELECT * FROM ticket_missing_fields WHERE ticket_id = ?", (ticket["id"],),
    )}
    for field in COMPLETION_FIELDS:
        if field not in missing and field not in existing:
            continue
        status = "filled" if field not in missing else "ignored" if field in protected else "missing"
        row = existing.get(field)
        if row is None:
            connection.execute(
                "INSERT INTO ticket_missing_fields (ticket_id, field_name, status, created_at) VALUES (?, ?, ?, ?)",
                (ticket["id"], field, status, _now_text()),
            )
        elif row["status"] != status:
            connection.execute(
                "UPDATE ticket_missing_fields SET status = ?, filled_at = ? WHERE id = ?",
                (status, _now_text() if status == "filled" else None, row["id"]),
            )


def _complete_conversation(connection, ticket: dict, actor="system", prefix="") -> None:
    text = prefix + "五项必需信息已补齐。" + ("原人工审核仍需运营确认。" if ticket["review_required"] else "请运营核对后继续处理。")
    message_id = _add_message(connection, ticket["id"], "assistant", text)
    events.create_event(connection, ticket["id"], "conversation_completed", actor,
                        {"completion_status": "incomplete"}, {"completion_status": "complete", "message_id": message_id},
                        reason="完成本次信息补全；未自动派单或解除人工审核。")


def _ask_conversation(connection, ticket: dict, prefix: str = "") -> None:
    from modules.conversation import generate_questions

    remaining = missing_ticket_fields(ticket)
    protected = _manual_empty_fields(connection, ticket["id"])
    actionable = [field for field in remaining if field not in protected]
    questions = generate_questions(ticket, actionable)
    question = questions[0] if questions else "剩余空字段由人工明确清空，已暂停自动追问，请通过人工审核处理。"
    assistant_id = _add_message(connection, ticket["id"], "assistant", prefix + question)
    events.create_event(connection, ticket["id"], "conversation_question_generated", "system", {},
                        {"message_id": assistant_id, "question_field": actionable[0] if actionable else None,
                         "missing_fields": remaining}, reason="生成本轮补问，完整度不替代人工审核。")


def _sync_after_manual_review(connection, before: dict, after: dict) -> None:
    if not connection.execute("SELECT 1 FROM conversation_messages WHERE ticket_id = ? LIMIT 1", (before["id"],)).fetchone():
        return
    _sync_missing_fields(connection, after)
    for field in missing_ticket_fields(before):
        if field not in missing_ticket_fields(after):
            events.create_event(connection, before["id"], "field_filled", "operator",
                                {field: before[field]}, {field: after[field], "source": "manual"},
                                reason="人工审核补齐已开始跟踪的字段。")
    if missing_ticket_fields(before) and not missing_ticket_fields(after):
        _complete_conversation(connection, after, "operator")
    elif missing_ticket_fields(before) != missing_ticket_fields(after):
        _ask_conversation(connection, after, "人工审核已更新工单。")


def save_conversation_turn(
    ticket_id: str, expected_ticket: dict, expected_message_id: int,
    updates: dict, message: str | None, source: str,
) -> dict | None:
    """原工单、缺失跟踪、消息和事件在同一个事务内提交，任一失败全部回滚。"""
    from modules.conversation import validate_reply_fields

    if source not in ("ai", "rule", "demo") or not isinstance(updates, dict) or set(updates) - set(COMPLETION_FIELDS):
        raise ValueError("补全字段或来源不正确。")
    init_db()
    with _connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            return None
        before = _ticket_from_row(row)
        last = connection.execute("SELECT MAX(id) FROM conversation_messages WHERE ticket_id = ?", (ticket_id,)).fetchone()[0] or 0
        if before != expected_ticket or last != expected_message_id:
            raise ValueError("工单或对话已更新，请刷新后重新提交回复。")
        if not missing_ticket_fields(before):
            raise ValueError("当前必需字段已完整，无需继续补全。")
        protected = _manual_empty_fields(connection, ticket_id)
        missing = [field for field in missing_ticket_fields(before) if field not in protected]
        if message is None:
            if last:
                return before  # 重复点击开始不重复创建消息或事件。
            system_id = _add_message(connection, ticket_id, "system", "开始补全五项必需信息；只补空值，不覆盖已有值或人工明确清空的字段。")
            events.create_event(connection, ticket_id, "conversation_started", "operator", {},
                                {"message_id": system_id, "missing_fields": missing_ticket_fields(before)}, reason="运营开始信息补全。")
            updates = {}
        else:
            if not last or not isinstance(message, str) or not message.strip() or len(message) > 2000:
                raise ValueError("请先开始补全，再提交有效客户回复。")
            updates = validate_reply_fields(updates, message, missing)
            message_id = _add_message(connection, ticket_id, "user", message)
        after = {**before, **updates}
        for field, value in updates.items():
            # field 已限制为五项常量，不接受用户传入的任意 SQL 标识符。
            connection.execute(f"UPDATE tickets SET {field} = ? WHERE id = ?", (value, ticket_id))
            events.create_event(connection, ticket_id, "field_filled", "system",
                                {field: before[field]}, {field: value, "source": source, "message_id": message_id},
                                reason="根据客户本轮回复补充空字段，保留原解析快照。")
        remaining = missing_ticket_fields(after)
        if message is not None and not before["review_required"] and before["status"] in ("待处理", "待补充信息", "待确认"):
            status = "待补充信息" if set(remaining) - {"appointment_time"} else "待确认"
            if status != before["status"]:
                connection.execute("UPDATE tickets SET status = ? WHERE id = ?", (status, ticket_id))
                after["status"] = status
                events.create_event(connection, ticket_id, "status_changed", "system",
                                    {"status": before["status"]}, {"status": status}, reason="补充后重新判断信息完整性。")
        _sync_missing_fields(connection, after)
        prefix = "AI不可用，使用规则补全。" if source == "rule" else "本轮使用演示规则。" if source == "demo" else ""
        if not remaining:
            _complete_conversation(connection, after, prefix=prefix)
        else:
            if message is not None:
                prefix += "本轮未提取到可写入的有效空字段，请提供明确内容。" if not updates else "本轮已补充信息。"
            _ask_conversation(connection, after, prefix if message is not None else "")
        return after


def get_completion_metrics(tickets: list[dict]) -> dict:
    """完成数限于有补全过程且当前完整的工单；平均字段数包含开始后尚未补齐的工单。"""
    init_db()
    with _connection() as connection:
        rows = connection.execute(
            """SELECT ticket_id, field_name FROM ticket_missing_fields
               WHERE status = 'filled' AND filled_at IS NOT NULL"""
        ).fetchall()
        started = {row[0] for row in connection.execute("SELECT DISTINCT ticket_id FROM conversation_messages")}
    by_id = {ticket["id"]: ticket for ticket in tickets}
    filled = sum(row["ticket_id"] in by_id and row["field_name"] not in missing_ticket_fields(by_id[row["ticket_id"]]) for row in rows)
    return {
        "incomplete_ticket_count": sum(bool(missing_ticket_fields(ticket)) for ticket in tickets),
        "completed_conversation_count": sum(ticket["id"] in started and not missing_ticket_fields(ticket) for ticket in tickets),
        "avg_completed_fields": filled / len(started) if started else None,
    }


def get_appointment_reference_time(ticket: dict) -> str | None:
    """补充/人工修正的相对预约日期以该次事件日期为准，不修改客户原文。"""
    init_db()
    with _connection() as connection:
        rows = connection.execute(
            """SELECT after_data, created_at FROM ticket_events WHERE ticket_id = ?
               AND event_type IN ('field_filled', 'ticket_reviewed') ORDER BY id DESC""", (ticket["id"],),
        )
        for row in rows:
            after = json.loads(row["after_data"])
            if "appointment_time" in after:
                return row["created_at"] if after["appointment_time"] == ticket.get("appointment_time") else None
    return None
