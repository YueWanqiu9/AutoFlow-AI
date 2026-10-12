"""创建基础工单；使用字典表示工单，方便后续解析和存储。"""

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from database.db import get_ticket, save_ticket, update_ticket_after_review
from modules.rules import CRITICAL_FIELDS, check_confidence, check_ticket_status

TICKET_FIELDS = ("service_type", "appointment_time", "address", "customer_name", "phone", "urgency")


def create_ticket(customer_input: str, parsed_fields: dict | None = None) -> dict:
    """保存解析值及自评分，判断审核需求；兼容历史无分数的字典输入。"""
    if not customer_input.strip():
        raise ValueError("请先输入客户需求，再创建工单。")

    fields = parsed_fields or {}
    has_confidence = any(isinstance(fields.get(field), dict) for field in TICKET_FIELDS)
    values = {}
    for field in TICKET_FIELDS:
        item = fields.get(field, "")
        value = item.get("value", "") if isinstance(item, dict) else item
        if not isinstance(value, str):
            raise ValueError(f"工单字段 {field} 必须是字符串。")
        values[field] = value.strip()
    parsed_result = {field: fields[field] for field in TICKET_FIELDS if field in fields}
    review_required = has_confidence and check_confidence(parsed_result) == "人工审核"
    ticket = {
        "id": str(uuid4()),
        "customer_input": customer_input,
        **values,
        "status": "待处理",
        "created_time": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "confidence_score": json.dumps(parsed_result, ensure_ascii=False, allow_nan=False) if has_confidence else "{}",
        "review_required": review_required,
        "token_used": getattr(parsed_fields, "token_used", 0),
        "response_time": getattr(parsed_fields, "response_time", None),
    }
    ticket["status"] = "人工审核" if review_required else check_ticket_status(ticket)
    save_ticket(ticket)
    return ticket


def get_saved_parse_result(ticket: dict) -> dict:
    """读取原 AI 解析快照，历史工单没有记录时返回空字典。"""
    try:
        result = json.loads(ticket.get("confidence_score", "{}"))
    except (json.JSONDecodeError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}


def review_ticket(ticket_id: str, reviewed_fields: dict) -> dict | None:
    """人工核对或修正字段后解除待审标记，保留原 AI 自评分不变。"""
    values = {}
    for field in TICKET_FIELDS:
        value = reviewed_fields.get(field)
        if not isinstance(value, str):
            raise ValueError("请核对全部审核字段，字段值必须为文本。")
        values[field] = value.strip()
    ticket = get_ticket(ticket_id)
    if not ticket or not ticket["review_required"]:
        return None
    status = check_ticket_status(values)
    if any(not values[field] for field in CRITICAL_FIELDS):
        status = "待补充信息"
    if not update_ticket_after_review(ticket_id, values, status):
        return None
    return {**ticket, **values, "status": status, "review_required": False}
