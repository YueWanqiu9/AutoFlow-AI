"""运营看板统计，分别从工单数据库和 AI 调用日志读取指标。"""

from collections import Counter
from datetime import datetime

from database.db import get_all_tickets
from modules.ai_logging import get_ai_call_stats
from modules.automation import REPORT_TIMEZONE


def get_dashboard_stats() -> dict:
    """返回工单分布及 AI 调用指标；日志不可用时保留工单统计。"""
    today = datetime.now(REPORT_TIMEZONE).date()
    tickets = get_all_tickets()
    today_new_tickets = 0
    for ticket in tickets:
        created_at = datetime.fromisoformat(ticket["created_time"])
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=REPORT_TIMEZONE)
        if created_at.astimezone(REPORT_TIMEZONE).date() == today:
            today_new_tickets += 1

    status_counts = Counter(ticket["status"].strip() or "未设置" for ticket in tickets)
    service_type_counts = Counter(ticket["service_type"].strip() or "未识别" for ticket in tickets)
    try:
        ai_metrics = get_ai_call_stats()
    except (OSError, ValueError):
        ai_metrics = {
            "ai_call_count": None, "avg_response_time": None,
            "ai_success_count": None, "ai_success_rate": None,
        }
        ai_metrics_error = "AI调用日志暂不可读取"
    else:
        ai_metrics_error = None
    return {
        "date": today.isoformat(),
        "total_tickets": len(tickets),
        "today_new_tickets": today_new_tickets,
        "status_counts": dict(status_counts.most_common()),
        "service_type_counts": dict(service_type_counts.most_common()),
        "review_required_count": sum(ticket.get("review_required") is True for ticket in tickets),
        "ai_call_count": ai_metrics["ai_call_count"],
        "avg_response_time": ai_metrics["avg_response_time"],
        "ai_success_count": ai_metrics["ai_success_count"],
        "ai_success_rate": ai_metrics["ai_success_rate"],
        "ai_metrics_error": ai_metrics_error,
    }
