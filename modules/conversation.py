"""逐轮补齐已有工单的空字段；问题使用稳定模板，回复可用 AI 或本地规则解析。"""

import re

import config
from database import db
from modules.ai_client import request_conversation
from modules.rules import COMPLETION_FIELDS, missing_ticket_fields

FIELD_LABELS = {"customer_name": "客户姓名", "phone": "联系电话", "address": "服务地址",
                "service_type": "服务类型", "appointment_time": "预约时间"}
QUESTIONS = {field: f"请补充{label}，可回复“{label}：具体内容”。" for field, label in FIELD_LABELS.items()}
PHONE_PATTERN = r"(?:\+?86[- ]?)?1[3-9]\d{9}|0\d{2,3}-?\d{7,8}|演示电话\d{3}"
ALIASES = {"客户姓名": "customer_name", "姓名": "customer_name", "我叫": "customer_name",
           "联系电话": "phone", "手机号": "phone", "电话": "phone", "手机": "phone",
           "服务地址": "address", "地址": "address", "服务类型": "service_type", "服务项目": "service_type",
           "预约时间": "appointment_time", "服务时间": "appointment_time", "时间": "appointment_time"}
LABEL_PATTERN = re.compile(r"(?:^|(?<=[，,；;。\n\r\t ]))(" + "|".join(sorted(ALIASES, key=len, reverse=True)) + r")\s*(?:[:：]|是)?\s*")


def detect_missing_fields(ticket: dict) -> dict:
    return {"missing": missing_ticket_fields(ticket)}


def generate_questions(ticket: dict, missing_fields) -> list[str]:
    """按固定字段顺序生成问题；忽略未知字段和已有值的字段，不调用模型。"""
    requested = missing_fields.get("missing", []) if isinstance(missing_fields, dict) else missing_fields
    return [QUESTIONS[field] for field in missing_ticket_fields(ticket) if field in requested]


def _labelled_values(message: str) -> dict[str, list[str]]:
    matches = list(LABEL_PATTERN.finditer(message))
    values = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(message)
        value = message[match.end():end].strip(" ，,；;。\n\r\t")
        values.setdefault(ALIASES[match.group(1)], []).append(value)
    return values


def validate_reply_fields(candidates: dict, message: str, missing: list[str]) -> dict:
    """只接受回复中的原文证据；拒绝不确定、冲突、无效格式和非缺失字段。"""
    if not isinstance(candidates, dict):
        return {}
    labelled = _labelled_values(message)
    result = {}
    for field in COMPLETION_FIELDS:
        value = candidates.get(field)
        if field not in missing or not isinstance(value, str):
            continue
        value = value.strip()
        if not value or len(value) > 200 or value not in message:
            continue
        if re.search(r"不知道|不清楚|不确定|未确定|待定|暂时|暂无|没有|不提供|不方便|稍后|拒绝|无法|未知|未提供|未填写|待补充|可能|也许|大概|不详|未命名", value):
            continue
        if value.lower() in ("null", "none", "n/a", "无"):
            continue
        position = message.find(value)
        prefix = re.split(r"[，,；;。\n]", message[:position])[-1]
        suffix = message[position + len(value):]
        if re.search(r"不是|不要|别用|错误|可能|也许|不确定|不能用|别填|不用", prefix) or re.match(r"(?:不对|有误|是错的|不能用)", suffix):
            continue
        explicit = set(labelled.get(field, []))
        if len(explicit) > 1 or (explicit and value not in explicit):
            continue
        if field == "phone":
            numbers = set(re.findall(r"(?<!\d)(?:" + PHONE_PATTERN + r")(?!\d)", message))
            if not re.fullmatch(PHONE_PATTERN, value) or len(numbers) != 1:
                continue
        elif field == "customer_name":
            if not re.fullmatch(r"[\u4e00-\u9fffA-Za-z·.' -]{2,40}", value) or re.match(r"请|你|我想|能否|什么时候", value):
                continue
        elif field == "appointment_time":
            if not re.search(r"\d|今天|明天|后天|周[一二三四五六日天]|星期|上午|下午|晚上|中午|[一二三四五六七八九十两]+点", value):
                continue
        elif len(value) < 2 or re.search(r"[？?]|忽略指令|系统提示|工单状态", value):
            continue
        result[field] = value
    return result


def rule_parse_reply(message: str, missing: list[str], question_field: str | None) -> dict:
    labelled = _labelled_values(message)
    candidates = {field: values[0] for field, values in labelled.items() if len(set(values)) == 1}
    numbers = set(re.findall(r"(?<!\d)(?:" + PHONE_PATTERN + r")(?!\d)", message))
    if "phone" not in labelled and len(numbers) == 1:
        candidates["phone"] = next(iter(numbers))
    if not labelled and not numbers and question_field in missing:
        # 短回复只对应上一轮实际提出的问题；长文本无法可靠判断时继续询问。
        value = message.strip(" 。\n\r\t")
        if len(value) <= 60 and not re.search(r"[，,；;\n]|我想|请问|你好|谢谢|天气|不知道|不清楚", value):
            candidates[question_field] = value
    return validate_reply_fields(candidates, message, missing)


def start_conversation(ticket_id: str) -> dict | None:
    ticket = db.get_ticket(ticket_id)
    if ticket is None:
        return None
    if not missing_ticket_fields(ticket):
        raise ValueError("当前必需字段已完整，无需开始补全。")
    state = db.get_conversation_state(ticket_id)
    return db.save_conversation_turn(ticket_id, ticket, state["last_message_id"], {}, None, "rule")


def process_reply(ticket_id: str, message: str, *, expected_message_id: int | None = None) -> dict | None:
    if not isinstance(message, str) or not message.strip():
        raise ValueError("请先输入客户回复。")
    if len(message) > 2000:
        raise ValueError("客户回复请控制在2000字以内。")
    ticket = db.get_ticket(ticket_id)
    if ticket is None:
        return None
    state = db.get_conversation_state(ticket_id)
    if not state["messages"]:
        raise ValueError("请先点击开始补全。")
    if expected_message_id is not None and expected_message_id != state["last_message_id"]:
        raise ValueError("对话已更新，请查看最新问题后重新提交。")
    missing = [field for field in missing_ticket_fields(ticket) if field not in state["protected_fields"]]
    if not missing:
        raise ValueError("没有可自动补充的空字段；人工清空字段请通过人工审核修改。")
    question_field = state["question_field"] if state["question_field"] in missing else None
    fallback = rule_parse_reply(message, missing, question_field)
    source, updates = "rule", fallback
    demo = config.DEMO_MODE or ticket.get("priority_source") == "demo" or str(ticket_id).startswith("DEMO-")
    if demo:
        source = "demo"
    else:
        try:
            output = request_conversation({"reply": message, "missing_fields": missing,
                                           "question_field": question_field}, ticket_id)
            if output is not None:
                updates = {**validate_reply_fields(output, message, missing), **fallback}
                source = "ai"
        except Exception:
            # 封装或配置异常同样保留规则路径，不阻止记录本轮回复。
            pass
    return db.save_conversation_turn(ticket_id, ticket, state["last_message_id"], updates, message.strip(), source)
