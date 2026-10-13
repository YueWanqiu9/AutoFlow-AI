"""规则分级与可选 AI 解释；不是预测模型，不读写数据库。"""

import re
from datetime import datetime, timedelta, timezone

import config
from modules.ai_client import explain_priority

LEVELS = ("高", "普通", "低", "待人工分级")
FINAL_LEVELS = ("高", "普通", "低")
NEAR_SERVICE_HOURS = 24
TIMEZONE = timezone(timedelta(hours=8))
HIGH_RISK_TERMS = ("高风险", "高空作业", "燃气泄漏", "漏电", "危险品处理", "紧急转运")


def _text(ticket, field):
    value = ticket.get(field, "")
    return value.strip() if isinstance(value, str) else ""


def _positive_term(text, terms):
    """只匹配未被就近否定的词，不把“不紧急”当成紧急。"""
    for term in terms:
        for match in re.finditer(re.escape(term), text):
            prefix = re.split(r"[，。；、,;！!？?\n]", text[:match.start()])[-1]
            if not re.search(r"(?:不|无|没|未|非|不是|不涉及|无需|不用|不需要|没有)(?:任何|发生|出现|存在|进行|安排)?$", prefix):
                return term
    return None


def _number(text):
    if text.isdigit():
        return int(text)
    digits = {char: number for number, char in enumerate("零一二三四五六七八九")}
    digits["两"] = 2
    if "十" in text:
        left, right = text.split("十", 1)
        return digits.get(left, 1) * 10 + digits.get(right, 0)
    return digits.get(text, -1)


def _appointment_at(ticket):
    """相对日期使用最近一次预约补充/修正日期，无记录时使用需求创建日。"""
    text = _text(ticket, "appointment_time")
    if not text:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?", text):
            result = datetime.fromisoformat(text)
            return result.replace(tzinfo=TIMEZONE) if result.tzinfo is None else result.astimezone(TIMEZONE)
        date_match = re.match(r"(\d{4})[-年](\d{1,2})[-月](\d{1,2})日?", text)
        if date_match:
            day = datetime(*(int(value) for value in date_match.groups()), tzinfo=TIMEZONE)
            remainder = text[date_match.end():].strip()
        else:
            relative = re.match(r"(今天|明天|后天)", text)
            if not relative:
                return None
            created = datetime.fromisoformat(_text(ticket, "appointment_reference_time") or _text(ticket, "created_time"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=TIMEZONE)
            day = created.astimezone(TIMEZONE) + timedelta(days={"今天": 0, "明天": 1, "后天": 2}[relative[1]])
            remainder = text[relative.end():].strip()
        time_match = re.fullmatch(
            r"(凌晨|早上|上午|中午|下午|晚上)?\s*([\d零一二两三四五六七八九十]+)"
            r"(?::(\d{1,2})|[点时](半|[\d零一二三四五六七八九十]+分?)?)", remainder,
        )
        if not time_match:
            return None
        period, hour_text, colon_minutes, chinese_minutes = time_match.groups()
        hour = _number(hour_text)
        minute = int(colon_minutes) if colon_minutes is not None else (
            30 if chinese_minutes == "半" else _number(chinese_minutes.rstrip("分")) if chinese_minutes else 0
        )
        if period in ("下午", "晚上") and 1 <= hour < 12:
            hour += 12
        elif period == "中午" and 1 <= hour <= 2:
            hour += 12
        elif period in ("凌晨", "上午") and hour == 12:
            hour = 0
        return day.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except (ValueError, OverflowError):
        return None


def rule_priority(ticket):
    """高等级条件先判断，联系方式缺失不参与降级。"""
    if not isinstance(ticket, dict):
        return {"level": "待人工分级", "reason": "工单信息不足，请运营人员核对。", "source": "rule"}
    raw = _text(ticket, "customer_input")
    service = _text(ticket, "service_type")
    urgency = _text(ticket, "urgency")
    appointment = _text(ticket, "appointment_time")
    reasons = []
    if urgency == "紧急" or _positive_term(raw, ("紧急", "加急", "急需", "马上", "立即", "尽快")):
        reasons.append("客户明确表达紧急诉求")
    risk = _positive_term(f"{service}。{raw}", HIGH_RISK_TERMS)
    if risk:
        reasons.append(f"命中运营高风险服务规则“{risk}”，需人工核对")
    now = datetime.now(TIMEZONE)
    appointment_at = _appointment_at(ticket)
    if appointment_at is not None and timedelta(0) <= appointment_at - now <= timedelta(hours=NEAR_SERVICE_HOURS):
        reasons.append(f"服务时间在未来 {NEAR_SERVICE_HOURS} 小时内（{appointment_at:%Y-%m-%d %H:%M}）")
    if reasons:
        return {"level": "高", "reason": "；".join(reasons) + "。", "source": "rule"}
    if appointment_at is not None and appointment_at < now:
        return {"level": "待人工分级", "reason": "预约时间已过，请人工核对服务是否仍需安排。", "source": "rule"}
    flexible_time = appointment in ("不限", "时间不限", "没有时间要求", "无时间要求", "随时")
    no_deadline = flexible_time or any(term in raw for term in ("时间不限", "没有时间要求", "无时间要求", "不着急", "不急", "随时都行"))
    consultation = _positive_term(raw, ("咨询", "了解价格", "了解收费"))
    if consultation and (not appointment or flexible_time):
        return {"level": "低", "reason": "当前为非紧急咨询，尚无具体预约安排。", "source": "rule"}
    if no_deadline and (not appointment or flexible_time) and service:
        return {"level": "低", "reason": "客户明确无时间要求，未发现紧急或高风险条件。", "source": "rule"}
    if not service or not appointment:
        return {"level": "待人工分级", "reason": "服务类型或预约安排不足，需人工分级；缺少电话不会直接降低优先级。", "source": "rule"}
    if appointment_at is None:
        return {"level": "待人工分级", "reason": "预约时间尚不能精确识别，请人工核对是否临近；不猜测具体日期。", "source": "rule"}
    return {"level": "普通", "reason": "普通预约服务，未命中明确紧急或高风险条件。", "source": "rule"}


def suggest_priority(ticket):
    """保持三个返回字段；AI只解释规则，失败时原样返回规则结果。"""
    result = rule_priority(ticket)
    if config.DEMO_MODE or (isinstance(ticket, dict) and ticket.get("priority_source") == "demo"):
        return {**result, "source": "demo"}
    try:
        explanation = explain_priority(result, ticket.get("id") if isinstance(ticket, dict) else None)
    except Exception:
        explanation = None
    if isinstance(explanation, str) and explanation.strip():
        return {**result, "reason": result["reason"] + " AI说明：" + explanation, "source": "ai"}
    return result
