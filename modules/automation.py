"""按需生成运营日报，只读取工单，不调用 AI 或修改数据。"""

from collections import Counter
from datetime import datetime, timedelta, timezone

from database.db import get_all_tickets

REPORT_TIMEZONE = timezone(timedelta(hours=8))


def _summarize(tickets: list[dict]) -> dict[str, int]:
    counts = Counter(ticket["status"] for ticket in tickets)
    return {
        "total_tickets": len(tickets),
        "completed": counts["已完成"],
        "pending_information": counts["待补充信息"],
        "pending_confirmation": counts["待确认"],
        "pending_review": counts["人工审核"],
        "abnormal": counts["异常"],
        "pending": sum(
            counts[status]
            for status in ("待处理", "待补充信息", "待确认", "待派单", "服务中", "人工审核")
        ),
    }


def generate_daily_report() -> dict:
    """返回累计统计和北京时间今日新增工单的当前状态统计。"""
    today = datetime.now(REPORT_TIMEZONE).date()
    tickets = get_all_tickets()
    daily_tickets = []
    for ticket in tickets:
        created_at = datetime.fromisoformat(ticket["created_time"])
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=REPORT_TIMEZONE)
        if created_at.astimezone(REPORT_TIMEZONE).date() == today:
            daily_tickets.append(ticket)

    return {
        "report_date": today.isoformat(),
        "overall": _summarize(tickets),
        "today": _summarize(daily_tickets),
    }
