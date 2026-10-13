"""运营看板统计，分别从工单数据库和 AI 调用日志读取指标。"""

from collections import Counter
from datetime import datetime

from database.db import get_all_tickets, get_completion_metrics
from modules.ai_logging import get_ai_call_stats
from modules.automation import REPORT_TIMEZONE
from modules.staff import get_staff_metrics
from modules.matching import count_matchable_tickets
from modules.dispatch import get_dispatch_metrics
from modules.service_tracking import get_service_metrics


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
    priority_counts = dict.fromkeys(("高", "普通", "低", "待分级"), 0)
    high_priority_open_count = 0
    advice_decision_counts = dict.fromkeys(("未评审", "已采纳", "已修改", "已拒绝"), 0)
    advice_labels = {"unreviewed": "未评审", "accepted": "已采纳", "modified": "已修改", "rejected": "已拒绝"}
    for ticket in tickets:
        priority = ticket.get("priority_final")
        label = priority if priority in ("高", "普通", "低") else "待分级"
        priority_counts[label] += 1
        if label == "高" and ticket["status"].strip() not in ("已完成", "已取消"):
            high_priority_open_count += 1
        if ticket.get("advice_json") and ticket.get("advice_decision") in advice_labels:
            advice_decision_counts[advice_labels[ticket["advice_decision"]]] += 1
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
    try:
        matchable_ticket_count = count_matchable_tickets(tickets)
        matching_metrics_error = None
    except ValueError:
        # 技能 JSON 损坏时不能把“无法计算”显示为没有可匹配工单。
        matchable_ticket_count = None
        matching_metrics_error = "人员资料格式不正确，匹配统计暂不可用。"
    return {
        **get_completion_metrics(tickets),
        **get_staff_metrics(),
        **get_dispatch_metrics(),
        **get_service_metrics(),
        "matchable_ticket_count": matchable_ticket_count,
        "matching_metrics_error": matching_metrics_error,
        "date": today.isoformat(),
        "total_tickets": len(tickets),
        "today_new_tickets": today_new_tickets,
        "status_counts": dict(status_counts.most_common()),
        "service_type_counts": dict(service_type_counts.most_common()),
        "priority_counts": priority_counts,
        "high_priority_open_count": high_priority_open_count,
        "advice_decision_counts": advice_decision_counts,
        "advice_generated_count": sum(advice_decision_counts.values()),
        "review_required_count": sum(ticket.get("review_required") is True for ticket in tickets),
        "ai_call_count": ai_metrics["ai_call_count"],
        "avg_response_time": ai_metrics["avg_response_time"],
        "ai_success_count": ai_metrics["ai_success_count"],
        "ai_success_rate": ai_metrics["ai_success_rate"],
        "ai_metrics_error": ai_metrics_error,
    }
