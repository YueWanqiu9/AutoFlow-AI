"""处理建议生成与展示格式；不读写数据库，不改变工单状态。"""

import config
from modules.ai_client import request_advice, validate_advice_content

ADVICE_LABELS = {
    "missing_information": "待补信息", "next_steps": "处理步骤", "attention_points": "注意事项",
}
DECISION_LABELS = {
    "unreviewed": "未评审", "accepted": "已采纳", "modified": "已修改", "rejected": "已拒绝",
}


def validate_advice(advice: dict) -> dict:
    """统一验证模型、规则和持久化边界的四字段结构。"""
    if (not isinstance(advice, dict) or set(advice) != {*ADVICE_LABELS, "source"}
            or advice["source"] not in ("ai", "rule", "demo")):
        raise ValueError("处理建议格式错误。")
    content = validate_advice_content({field: advice[field] for field in ADVICE_LABELS})
    return {**content, "source": advice["source"]}


def format_advice(advice: dict) -> str:
    """将完整建议转成可采纳、可编辑的纯文本，不丢失待补项或注意事项。"""
    advice = validate_advice(advice)
    return "\n\n".join(
        label + "：\n" + ("\n".join(f"{index}. {item}" for index, item in enumerate(advice[field], 1)) or "无")
        for field, label in ADVICE_LABELS.items()
    )


def rule_advice(ticket: dict) -> dict:
    """根据已保存信息提供基础运营核对模板，不假定已联系客户或已执行服务。"""
    fields = {"service_type": "服务类型", "appointment_time": "预约时间", "address": "服务地址", "phone": "联系电话"}
    missing = [f"需要补充{label}" for field, label in fields.items()
               if not isinstance(ticket.get(field), str) or not ticket[field].strip()]
    steps = ["联系客户确认服务需求", "核对预约时间及服务地址", "记录核对结果，人工确定后续处理方案"]
    if missing:
        steps.insert(0, "通过现有可用渠道补齐待补信息；无联系渠道时先核查原始需求记录")
    attention = ["建议仅供运营核对，采纳后仍需人工执行。"]
    if ticket.get("review_required"):
        attention.append("工单仍需人工审核，处理建议不能替代字段审核。")
    if ticket.get("priority_final") == "高":
        attention.append("最终优先级为高，需要优先跟进。")
    elif ticket.get("priority_suggested") == "高" and not ticket.get("priority_final"):
        attention.append("系统建议高优先级，需运营确认等级并核对紧急程度。")
    if ticket.get("status") in ("已完成", "已取消"):
        steps = ["核查工单处理记录与关闭原因", "如需后续处理，先由运营确认是否有新的需求"]
        attention.append("工单当前已关闭，不自动重新启动服务。")
    return {"missing_information": missing, "next_steps": steps, "attention_points": attention, "source": "rule"}


def generate_advice(ticket: dict) -> dict:
    """返回固定结构；演示离线，真实调用失败时返回明确标记的规则建议。"""
    ticket = ticket if isinstance(ticket, dict) else {}
    fallback = rule_advice(ticket)
    if config.DEMO_MODE or ticket.get("priority_source") == "demo" or str(ticket.get("id", "")).startswith("DEMO-"):
        return {**fallback, "source": "demo"}
    # 仅发送运营所需摘要，不发送原文、客户姓名、具体地址或联系电话。
    context = {field: ticket.get(field) for field in (
        "service_type", "appointment_time", "status", "review_required", "priority_suggested", "priority_final",
    )}
    context["rule_advice"] = {field: fallback[field] for field in ADVICE_LABELS}
    try:
        output = request_advice(context, ticket.get("id"))
        if output is not None:
            result = validate_advice({**output, "source": "ai"})
            # 明确的缺失项不可因模型遗漏而消失。
            result["missing_information"] = list(dict.fromkeys(fallback["missing_information"] + result["missing_information"]))
            return validate_advice(result)
    except Exception:
        # 包括配置、API、格式异常；生成失败不触碰工单和状态。
        pass
    fallback["attention_points"].insert(0, "AI不可用，使用规则建议。")
    return fallback
