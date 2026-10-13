"""创建基础工单；使用字典表示工单，方便后续解析和存储。"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import config
from database import db
from database.db import get_ticket, save_ticket, update_ticket_after_review
from modules.rules import CRITICAL_FIELDS, check_confidence, check_ticket_status
from modules.priority import rule_priority, suggest_priority
from modules.advisor import generate_advice

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
        **dict.fromkeys(db.PRIORITY_FIELDS),
        **dict.fromkeys(db.ADVICE_FIELDS),
        "advice_decision": "unreviewed",
    }
    ticket["status"] = "人工审核" if review_required else check_ticket_status(ticket)
    # 数据库在同一事务内保存工单和创建事件，任一写入失败都会回滚。
    save_ticket(ticket)
    try:
        # 创建后先给出本地规则建议，不增加网络依赖。AI解释由明确按钮触发。
        generated = generate_ticket_priority(
            ticket["id"], use_ai=False,
            demo=config.DEMO_MODE or getattr(parsed_fields, "call_status", "") == "demo",
        )
        if generated is not None:
            ticket = generated
    except (sqlite3.Error, OSError, ValueError):
        # 创建已提交；建议事件事务失败不会抹掉客户需求，可在详情中重试。
        pass
    return ticket


def generate_ticket_priority(ticket_id: str, *, use_ai: bool = True, demo: bool = False) -> dict | None:
    """网络调用在事务外完成，保存建议时再次核对原工单快照。"""
    ticket = get_ticket(ticket_id)
    if ticket is None:
        return None
    reference_time = db.get_appointment_reference_time(ticket)
    priority_input = {**ticket, "appointment_reference_time": reference_time} if reference_time else ticket
    suggestion = suggest_priority(priority_input) if use_ai else rule_priority(priority_input)
    if demo:
        suggestion = {**suggestion, "source": "demo"}
    return db.save_priority_suggestion(ticket_id, suggestion, ticket)


def confirm_priority(ticket_id: str, priority: str, reason: str) -> dict | None:
    """运营确认或调整最终优先级，保留系统建议与判断依据。"""
    return db.confirm_ticket_priority(ticket_id, priority, reason)


def generate_ticket_advice(ticket_id: str) -> dict | None:
    """仅显式操作时生成建议；已评审的最终方案不被新请求覆盖。"""
    ticket = get_ticket(ticket_id)
    if ticket is None:
        return None
    if ticket.get("advice_decision") not in (None, "unreviewed"):
        raise ValueError("已有人工评审结果，请直接修改最终方案，不能重新生成覆盖。")
    advice = generate_advice(ticket)
    return db.save_advice_suggestion(ticket_id, advice, ticket)


def confirm_advice(
    ticket_id: str, decision: str, final_advice: str, reason: str, *, expected_ticket: dict | None = None,
) -> dict | None:
    """保存运营的方案与原因；页面传入展示快照，防止并发确认过期内容。"""
    return db.confirm_ticket_advice(ticket_id, decision, final_advice, reason, expected_ticket=expected_ticket)


def change_ticket_status(ticket_id: str, status: str) -> bool:
    """修改状态；审核限制和事件写入统一由数据库事务保证。"""
    return db.update_ticket_status(ticket_id, status)


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
    # 审核字段、状态与审核事件一起提交，不额外开启事件事务。
    if not update_ticket_after_review(ticket_id, values, status):
        return None
    return {**ticket, **values, "status": status, "review_required": False}
