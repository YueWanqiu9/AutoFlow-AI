"""SQLite 工单存储，每次操作使用独立连接并在结束时关闭。"""

import math
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "autoflow.db"


@contextmanager
def _connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
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


def save_ticket(ticket: dict) -> None:
    """保存工单；响应耗时以秒为单位，未测量时保存为 NULL。"""
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
                confidence_score, review_required, token_used, response_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
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
            "SELECT review_required FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if row is None:
            return False
        if row["review_required"] == "true":
            raise ValueError("该工单需要先完成人工审核。")

        status = status.strip()
        cursor = connection.execute(
            "UPDATE tickets SET status = ?, review_required = ? WHERE id = ?",
            (status, "true" if status == "人工审核" else "false", ticket_id),
        )
        return cursor.rowcount > 0
