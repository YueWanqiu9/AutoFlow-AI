"""人员匹配的集中规则：技能映射、区域文本判断与四项评分，不调用 AI。"""

from modules.staff import parse_skills

RULE_VERSION = "4B-1.1"
SCORES = {"skill_full": 40, "skill_partial": 20, "area_same": 30, "area_adjacent": 15, "base": 10}
STATUS_SCORES = {"available": 20, "busy": 5, "inactive": 0}
CANDIDATE_STATUSES = ("available", "busy")
CLOSED_TICKET_STATUSES = ("已完成", "已取消")

# 工单服务类型与人员技能共用同一别名表，不用模糊包含推断资格。
SKILL_ALIASES = {
    "护理员上门": "老人护理", "上门护理": "老人护理", "养老护理": "老人护理", "老人护理": "老人护理",
    "助浴服务": "助浴", "助浴": "助浴",
    "康复服务": "康复辅助", "康复辅助": "康复辅助",
    "保洁服务": "居家保洁", "家庭保洁": "居家保洁", "居家保洁": "居家保洁",
    "上门维修": "维修服务", "维修服务": "维修服务",
}
# 部分相关只表示候选线索，不等于具备所需资质。按需求方向显式配置。
RELATED_SKILLS = {
    "老人护理": {"助浴", "康复辅助"},
    "助浴": {"老人护理"},
    "康复辅助": {"老人护理"},
    "维修服务": {"家电维修"},
    "家电维修": {"维修服务"},
}

# 仅处理已列出的地址写法，不根据区域名称推断地理关系。
AREA_ALIASES = {
    "玉溪市红塔区": "红塔区",
    "云南省玉溪市红塔区": "红塔区",
    "云南省玉溪市": "玉溪市",
}
# 无已确认的业务清单，默认不把不同区域当成相邻。
# 配置格式：(("区域甲", "区域乙"), ...)，每对关系双向生效。
ADJACENT_AREAS = ()
AREA_SUFFIXES = ("省", "市", "区", "县", "州", "旗", "镇", "乡", "街道")


def _text(value) -> str:
    return "".join(value.split()) if isinstance(value, str) else ""


def normalize_skill(value: str) -> str:
    text = _text(value)
    return SKILL_ALIASES.get(text, text)


def get_required_skill(ticket: dict) -> str:
    """只读取结构化服务类型；未登记的新类型仍可做精确技能匹配。"""
    return normalize_skill(ticket.get("service_type"))


def score_skill(required_skill: str, skills) -> tuple[int, str]:
    required = normalize_skill(required_skill)
    available = {normalize_skill(item) for item in parse_skills(skills)}
    if required and required in available:
        points = SCORES["skill_full"]
        return points, f"技能完全匹配：{required}（+{points}分）"
    related = sorted(available & RELATED_SKILLS.get(required, set()))
    if required and related:
        points = SCORES["skill_partial"]
        return points, f"技能部分相关：{'、'.join(related)}，需核实 {required} 的服务能力（+{points}分）"
    return 0, "技能不匹配或服务需求缺失（+0分）"


def normalize_area(value: str) -> str:
    text = _text(value)
    return AREA_ALIASES.get(text, text)


def _covers_address(area: str, address: str) -> bool:
    """只接受完整相等或明确的区域前缀；不在街道、小区名中搜索地名。"""
    aliases = {area, *(alias for alias, canonical in AREA_ALIASES.items() if canonical == area)}
    for prefix in aliases:
        if address == prefix:
            return True
        if prefix.endswith(AREA_SUFFIXES) and address.startswith(prefix):
            return True
        if any(address.startswith(prefix + separator) for separator in ("，", ",", "：", ":", "/")):
            return True
    return False


def score_area(address: str, service_area: str) -> tuple[int, str]:
    address, area = _text(address), normalize_area(service_area)
    if not address or not area:
        return 0, "客户地址或人员服务区域缺失，需补充（+0分）"
    if _covers_address(area, address):
        points = SCORES["area_same"]
        return points, f"区域匹配：地址位于所填服务区域前缀内（+{points}分）"
    for first, second in ADJACENT_AREAS:
        first, second = normalize_area(first), normalize_area(second)
        if (area == first and _covers_address(second, address)) or (area == second and _covers_address(first, address)):
            points = SCORES["area_adjacent"]
            return points, f"区域部分匹配：已配置的相邻服务区域（+{points}分）"
    return 0, "区域未匹配，需人工核实服务范围（+0分）"


def score_status(status: str) -> tuple[int, str]:
    points = STATUS_SCORES.get(status, 0)
    label = {"available": "当前可用", "busy": "当前忙碌，需另行确认可用性", "inactive": "已停用"}.get(status, "状态无效")
    return points, f"{label}（+{points}分）"


def score_staff(ticket: dict, person: dict) -> dict:
    """返回分项事实与总分；多项相关技能只取最高档，不重复加分。"""
    skill, skill_detail = score_skill(get_required_skill(ticket), person.get("skills"))
    area, area_detail = score_area(ticket.get("address"), person.get("service_area"))
    status, status_detail = score_status(person.get("status"))
    base = SCORES["base"]
    return {
        "skill_score": skill, "area_score": area, "status_score": status, "base_score": base,
        "score": skill + area + status + base,
        "match_details": [skill_detail, area_detail, status_detail, f"基础推荐分（+{base}分）"],
    }
