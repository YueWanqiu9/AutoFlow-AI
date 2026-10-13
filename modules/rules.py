"""独立于页面和 AI 的业务规则。"""

import math

CRITICAL_FIELDS = ("service_type", "address", "appointment_time", "phone")
CONFIDENCE_THRESHOLD = 0.7
COMPLETION_FIELDS = ("customer_name", "phone", "address", "service_type", "appointment_time")


def missing_ticket_fields(ticket: dict) -> list[str]:
    """完整度只反映五项是否有值，不代替置信度审核或资料真实性核验。"""
    return [field for field in COMPLETION_FIELDS
            if not isinstance(ticket.get(field), str) or not ticket[field].strip()]


def get_confidence_score(parsed_result: dict) -> float:
    """关键字段取最低自评分；缺失、空值或非法分数按零分处理。"""
    scores = []
    for field in CRITICAL_FIELDS:
        item = parsed_result.get(field)
        if not isinstance(item, dict):
            return 0.0
        value, confidence = item.get("value"), item.get("confidence")
        if not isinstance(value, str) or not value.strip():
            return 0.0
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            return 0.0
        if not 0 <= confidence <= 1 or not math.isfinite(confidence):
            return 0.0
        scores.append(float(confidence))
    return min(scores)


def check_confidence(parsed_result: dict) -> str:
    """任一关键字段低于 0.7 时进入人工审核，恰好 0.7 可通过。"""
    if get_confidence_score(parsed_result) < CONFIDENCE_THRESHOLD:
        return "人工审核"
    return "待确认"


def check_ticket_status(ticket: dict) -> str:
    """手机号或地址缺失时待补充，否则待确认；空白字符串也视为空。"""
    if not (ticket.get("phone") or "").strip():
        return "待补充信息"

    if not (ticket.get("address") or "").strip():
        return "待补充信息"

    return "待确认"
