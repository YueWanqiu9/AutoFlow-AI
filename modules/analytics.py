"""工单运营分析；读取 SQLite，保留原始解析与当前状态的不同统计口径。"""

import math
from collections import Counter
from datetime import datetime, timedelta

from database.db import get_all_tickets
from modules.automation import REPORT_TIMEZONE
from modules.rules import check_confidence
from modules.tickets import get_saved_parse_result


def get_operational_metrics() -> dict:
    """返回累计指标与北京时间最近 30 天新增趋势，不修改工单或调用 AI。"""
    today = datetime.now(REPORT_TIMEZONE).date()
    tickets = get_all_tickets()
    dates = [today - timedelta(days=offset) for offset in range(29, -1, -1)]
    daily_counts = dict.fromkeys(dates, 0)
    today_new = 0
    auto_passed = 0
    response_times = []

    for ticket in tickets:
        created_at = datetime.fromisoformat(ticket["created_time"])
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=REPORT_TIMEZONE)
        created_date = created_at.astimezone(REPORT_TIMEZONE).date()
        today_new += created_date == today
        if created_date in daily_counts:
            daily_counts[created_date] += 1

        # 人工审核会修改业务字段，但不会改变原始解析快照。
        parsed = get_saved_parse_result(ticket)
        if parsed and check_confidence(parsed) == "待确认":
            auto_passed += 1
        duration = ticket.get("response_time")
        if type(duration) in (int, float) and math.isfinite(duration) and duration >= 0:
            response_times.append(duration)

    statuses = Counter(ticket["status"].strip() or "未设置" for ticket in tickets)
    services = Counter(ticket["service_type"].strip() or "未识别" for ticket in tickets)
    total = len(tickets)
    completed = statuses.get("已完成", 0)
    return {
        "date": today.isoformat(),
        "total_tickets": total,
        "today_new_tickets": today_new,
        "completed_count": completed,
        "abnormal_count": statuses.get("异常", 0),
        "review_required_count": sum(ticket.get("review_required") is True for ticket in tickets),
        "ai_auto_passed_count": auto_passed,
        "avg_response_time": sum(response_times) / len(response_times) if response_times else None,
        "completion_rate": completed / total if total else 0.0,
        "ai_auto_processing_rate": auto_passed / total if total else 0.0,
        "status_counts": dict(statuses.most_common()),
        "service_type_counts": dict(services.most_common()),
        "daily_trend": [{"date": day.isoformat(), "new_tickets": count} for day, count in daily_counts.items()],
        "demo_ticket_count": sum(ticket["id"].startswith("DEMO-") for ticket in tickets),
    }
