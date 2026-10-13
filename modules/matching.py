"""实时人员规则排序，只读资源池；不保存推荐、不写事件、不调用 AI。"""

from modules import matching_rules
from modules.staff import list_staff


def match_staff(ticket: dict, *, staff_pool: list[dict] | None = None, include_demo: bool = True) -> list[dict]:
    """返回技能相关且未停用的候选，按总分降序、人员编号升序排列。

    默认读取当前人员；staff_pool 供页面和看板复用同一份内存快照，避免每张工单查询一次数据库。
    区域缺失或不匹配计零分，忙碌计五分，仍可作为待核实的候选；不代表可以派单。
    """
    if not isinstance(ticket, dict):
        raise ValueError("工单必须是字典。")
    if type(include_demo) is not bool:
        raise ValueError("是否包含演示人员必须是布尔值。")
    if not matching_rules.get_required_skill(ticket):
        return []
    people = list_staff(include_demo=include_demo) if staff_pool is None else staff_pool
    candidates = []
    for person in people:
        if person.get("status") not in matching_rules.CANDIDATE_STATUSES:
            continue
        if not include_demo and person.get("is_demo"):
            continue
        scores = matching_rules.score_staff(ticket, person)
        if scores["skill_score"] == 0:
            continue
        candidates.append({"staff_id": person["id"], "score": scores["score"], "match_details": scores["match_details"]})
    return sorted(candidates, key=lambda item: (-item["score"], item["staff_id"]))


def count_matchable_tickets(tickets: list[dict]) -> int:
    """当前未结束且至少有一名候选的工单数；含待核实候选和 demo，不是可派单数量。"""
    people = list_staff()
    return sum(
        bool(match_staff(ticket, staff_pool=people))
        for ticket in tickets
        if (ticket.get("status") or "").strip() not in matching_rules.CLOSED_TICKET_STATUSES
    )
