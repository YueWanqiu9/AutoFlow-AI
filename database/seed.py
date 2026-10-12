"""生成虚构业务记录；只向空库一次性写入，不调用 AI 或伪造 API 日志。"""

import json
from datetime import datetime, time, timedelta
from random import Random
from uuid import uuid4

from database import db
from modules.automation import REPORT_TIMEZONE
from modules.tickets import TICKET_FIELDS

SERVICE_TYPES = ("护理员上门", "居家保洁", "康复服务", "维修服务")
STATUSES = ("待确认", "人工审核", "服务中", "已完成", "异常")


def generate_demo_tickets(count: int = 120) -> list[dict]:
    """默认 120 条，四类服务均衡；至少一条覆盖最近 30 天的每一天。"""
    if type(count) is not int or count < 100:
        raise ValueError("演示数据数量必须是至少 100 的整数。")
    now = datetime.now(REPORT_TIMEZONE)
    random = Random(42)
    tickets = []
    for index in range(count):
        offset = index if index < 30 else random.randrange(30)
        day = now.date() - timedelta(days=offset)
        created_at = datetime.combine(day, time(hour=min(now.hour, 8 + index % 10)), tzinfo=REPORT_TIMEZONE)
        service = SERVICE_TYPES[index % len(SERVICE_TYPES)]
        status = STATUSES[index % len(STATUSES)]
        needs_review = status == "人工审核"
        # 部分已处理工单也保留低分原快照，表示人工审核后继续处理。
        confidence = 0.55 if needs_review or index % 7 == 0 else 0.92
        values = {
            "service_type": service,
            "appointment_time": f"{day.isoformat()} 下午两点",
            "address": f"模拟城区 演示小区{index % 12 + 1}号楼",
            "customer_name": f"模拟客户{index + 1:03d}",
            "phone": "" if needs_review else f"演示电话{index + 1:03d}",
            "urgency": "普通",
        }
        parsed = {
            field: {"value": value, "confidence": confidence if value else 0.0}
            for field, value in values.items()
        }
        tickets.append({
            "id": f"DEMO-{uuid4()}",
            "customer_input": f"[模拟业务数据] {values['customer_name']}预约{service}，地址{values['address']}。",
            **values,
            "status": status,
            "created_time": created_at.isoformat(timespec="seconds"),
            "confidence_score": json.dumps(parsed, ensure_ascii=False, allow_nan=False),
            "review_required": needs_review,
            "token_used": 0,
            "response_time": None,
        })
    return tickets


def seed_demo_data(count: int = 120) -> int:
    """空库时事务内插入整批记录并返回数量；已有任意工单时返回零。"""
    tickets = generate_demo_tickets(count)
    db.init_db()
    with db._connection() as connection:
        # 锁内检查空库，使两个同时点击的会话不会重复加载。
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]:
            return 0
        columns = ("id", "customer_input", *TICKET_FIELDS, "status", "created_time",
                   "confidence_score", "review_required", "token_used", "response_time")
        rows = [tuple(("true" if ticket[field] else "false") if field == "review_required" else ticket[field]
                      for field in columns) for ticket in tickets]
        # 与普通保存保持相同列集合；一条写入失败会回滚全部演示记录。
        connection.executemany(
            "INSERT INTO tickets (" + ", ".join(columns) + ") VALUES (" + ", ".join("?" for _ in columns) + ")",
            rows,
        )
    return len(tickets)
