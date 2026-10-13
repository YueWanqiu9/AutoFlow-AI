"""人员推荐解释：保留规则对象和分数，只组织可核实的说明与风险。"""

from copy import deepcopy

import config
from modules.ai_client import request_recommendation, validate_recommendation_content
from modules.matching import match_staff
from modules import matching_rules
from modules.staff import list_staff

SCHEDULE_RISK = "未接入实时排班，需要运营确认服务时间和时间冲突。"
ACTION_RISK = "此说明未执行派单，服务能力与实际可用性仍需人工确认。"


def _unavailable(message: str) -> dict:
    return {"recommended_staff": "", "reason": [message],
            "risk_notes": ["请重新查看最新人员推荐，再生成说明。", SCHEDULE_RISK, ACTION_RISK], "source": "rule"}


def _validate_candidates(ticket, candidates, people):
    """只核对输入快照是否仍符合规则，不改写传入的顺序、分数或详情。"""
    ids = set()
    for item in candidates:
        if (not isinstance(item, dict) or set(item) != {"staff_id", "score", "match_details"}
                or type(item["staff_id"]) is not int or item["staff_id"] <= 0 or item["staff_id"] in ids
                or type(item["score"]) is not int or not 0 <= item["score"] <= 100
                or not isinstance(item["match_details"], list)
                or not item["match_details"]
                or any(not isinstance(detail, str) or not detail.strip() for detail in item["match_details"])):
            raise ValueError("候选结果格式不正确。")
        ids.add(item["staff_id"])
    if candidates != sorted(candidates, key=lambda item: (-item["score"], item["staff_id"])):
        raise ValueError("候选顺序与规则不一致。")
    current = {item["staff_id"]: item for item in match_staff(ticket, staff_pool=people)}
    if any(current.get(item["staff_id"]) != item for item in candidates):
        raise ValueError("候选结果已过期或与当前规则不一致。")


def _explanation_facts(ticket: dict, candidate: dict, person: dict) -> tuple[list[str], list[str]]:
    """说明模板由实际分项与快照构建，不能从模型输出反推业务事实。"""
    scores = matching_rules.score_staff(ticket, person)
    reasons = [f"在本次候选列表中规则排序第一，总分为{candidate['score']}分。"]
    risks = [SCHEDULE_RISK, ACTION_RISK]
    if scores["skill_score"] == matching_rules.SCORES["skill_full"]:
        reasons.append("登记技能与本次服务需求完全匹配。")
    else:
        reasons.append("登记技能与本次需求部分相关，可作为待核实的候选。")
        risks.append("技能仅部分相关，需核实具体服务能力和相关资质。")
    if scores["area_score"] == matching_rules.SCORES["area_same"]:
        reasons.append("客户地址按区域文本规则匹配人员登记的服务范围。")
    elif scores["area_score"] == matching_rules.SCORES["area_adjacent"]:
        reasons.append("按已配置的相邻服务区域规则获得区域加分。")
        risks.append("属于相邻区域，需人工确认是否接受跨区域服务。")
    else:
        risks.append("服务区域未匹配或资料缺失，需人工核实服务范围。")
    if person["status"] == "available":
        reasons.append("人员当前登记状态为可用，具体预约档期尚未核验。")
    else:
        reasons.append("人员存在技能相关性，但登记状态忙碌，仅列为待核实候选。")
        risks.append("该人员登记状态为忙碌，不能据此认定可以立即接单。")
    if person["is_demo"]:
        risks.append("推荐对象为 demo 虚构人员，不代表真实员工或可用服务资源。")
    for field, label in (("appointment_time", "预约时间"), ("address", "服务地址")):
        if not isinstance(ticket.get(field), str) or not ticket[field].strip():
            risks.append(f"工单尚缺{label}，需要先补充并核对。")
    if ticket.get("review_required") or ticket.get("status") == "人工审核":
        risks.append("工单仍需人工审核，推荐说明不能替代字段审核。")
    if ticket.get("status") in matching_rules.CLOSED_TICKET_STATUSES:
        risks.append("工单已结束，本次说明不能作为重新启动服务的依据。")
    return reasons, risks


def generate_recommendation_explanation(ticket: dict, candidates: list[dict], *, staff_pool=None) -> dict:
    """只解释传入列表第一名；姓名由本地资料确定，模型没有更换对象或写库入口。

    staff_pool 供页面传入同一份匹配快照。输出始终为姓名、理由列表、风险列表和来源四字段。
    """
    if not isinstance(ticket, dict) or not isinstance(candidates, list):
        return _unavailable("工单或候选列表格式不正确，暂不生成解释。")
    if not candidates:
        return _unavailable("暂无候选人员，无法生成推荐说明。")
    ticket, candidates = deepcopy(ticket), deepcopy(candidates)
    try:
        people = deepcopy(list_staff() if staff_pool is None else staff_pool)
        _validate_candidates(ticket, candidates, people)
        people_by_id = {person["id"]: person for person in people}
        first = candidates[0]
        person = people_by_id[first["staff_id"]]
        reasons, risks = _explanation_facts(ticket, first, person)
        name = person["name"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError("人员姓名缺失。")
    except Exception:
        return _unavailable("人员资料不可读取，或候选结果与当前资料不一致。")
    fallback = {"recommended_staff": name, "reason": reasons, "risk_notes": risks, "source": "rule"}
    if (config.DEMO_MODE or ticket.get("priority_source") == "demo"
            or str(ticket.get("id", "")).startswith("DEMO-")
            or any(people_by_id[item["staff_id"]]["is_demo"] for item in candidates)):
        fallback["risk_notes"] = ["本次为演示模式或包含 demo 候选，使用规则说明，未调用AI。", *risks]
        return fallback

    # 不发送姓名、电话、客户原文、详细地址或预约原文；模型只解释规则事实。
    context = {
        "rule_version": matching_rules.RULE_VERSION,
        "ticket": {"required_skill": matching_rules.get_required_skill(ticket),
                   "appointment_time_provided": bool(isinstance(ticket.get("appointment_time"), str) and ticket["appointment_time"].strip()),
                   "address_provided": bool(isinstance(ticket.get("address"), str) and ticket["address"].strip()),
                   "review_required": bool(ticket.get("review_required"))},
        "candidates": candidates, "fixed_staff_id": first["staff_id"],
        "allowed_reasons": reasons, "allowed_risk_notes": risks,
    }
    try:
        output = request_recommendation(deepcopy(context), ticket.get("id"))
        if output is not None:
            content = validate_recommendation_content(output, context)
            return {"recommended_staff": name, "reason": content["reason"],
                    # 即使模型漏选某条风险，本地仍补回全部已确认的必要提示。
                    "risk_notes": list(dict.fromkeys(content["risk_notes"] + risks)), "source": "ai"}
    except Exception:
        pass
    return {**fallback, "risk_notes": ["AI不可用，使用规则推荐说明。", *risks]}
