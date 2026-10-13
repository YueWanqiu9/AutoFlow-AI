"""十步演示编排：在独立进程和独立 SQLite 中复用业务接口。

主进程不切换 db.DB_PATH，避免 Streamlit 的其他会话误写演示库。
只有完整执行成功的临时库才会发布；重复点击复用已完成案例。
"""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
DEMO_DB_PATH = ROOT / "data" / "demo_flow" / "flow.db"
DEMO_REQUEST = "我母亲82岁，行动不便，希望明天下午两点安排护理员上门照护，地址红塔区山水佳园。"
STEP_TITLES = (
    "AI解析需求", "生成工单", "发现缺失信息", "信息补全与审核", "确认高优先级",
    "生成与采纳处理建议", "推荐护理员", "确认派单", "接单并开始服务", "服务完成",
)
DEMO_TABLES = (
    "tickets", "ticket_events", "staff", "staff_events", "dispatches",
    "conversation_messages", "ticket_missing_fields", "demo_flow_snapshot",
)


class DemoFlowError(RuntimeError):
    """可以直接展示的演示错误，不包含子进程输出或配置。"""


def load_demo_flow(path=None):
    """只读已完成的演示快照；不补造记录或触发业务动作。"""
    path = Path(path or DEMO_DB_PATH).resolve()
    if not path.exists():
        return None
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            row = connection.execute("SELECT payload FROM demo_flow_snapshot WHERE id = 1 AND is_demo = 1").fetchone()
        result = json.loads(row[0]) if row else None
        if (not isinstance(result, dict) or result.get("is_demo") != 1
                or [step["title"] for step in result["steps"]] != list(STEP_TITLES)
                or result["dispatch"]["status"] != "completed"):
            raise ValueError("Incomplete demo")
        return result
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as error:
        raise DemoFlowError("演示记录读取失败，请检查独立演示库；工作台数据未受影响。") from error


def run_demo_flow():
    """明确点击时运行一次，跨会话串行创建，完成后一直复用同一案例。"""
    from database import db

    target = Path(DEMO_DB_PATH).resolve()
    if target == Path(db.DB_PATH).resolve():
        raise DemoFlowError("演示库不能与工作台数据库使用同一路径。")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # 单独的锁库不保存业务数据；防止多个浏览器同时创建两份演示。
        with closing(sqlite3.connect(target.with_suffix(".lock.db"), timeout=90)) as guard:
            with guard:
                guard.execute("BEGIN IMMEDIATE")
                saved = load_demo_flow(target)
                if saved is not None:
                    return saved
                with TemporaryDirectory(prefix="build-", dir=target.parent) as temporary:
                    output = Path(temporary) / "flow.db"
                    result = subprocess.run(
                        [sys.executable, "-m", "database.seed_demo_flow", "--build", str(output)],
                        cwd=ROOT, capture_output=True, timeout=60,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    if result.returncode != 0:
                        raise DemoFlowError("演示执行失败，没有保存未完成的案例。可再次点击重试。")
                    saved = load_demo_flow(output)
                    if saved is None:
                        raise DemoFlowError("演示未生成完整结果，请重试。")
                    os.replace(output, target)
                    return saved
    except (OSError, sqlite3.Error, subprocess.TimeoutExpired) as error:
        raise DemoFlowError("演示暂时无法完成，请检查数据目录权限或稍后重试；工作台数据未受影响。") from error


def _build_demo(path):
    """仅由子进程调用；强制离线，任何业务失败均不发布这个临时库。"""
    import config
    from database import db
    from modules import ai_logging
    from modules.advisor import format_advice
    from modules.ai_parser import ParsedRequest, REQUEST_FIELDS
    from modules.conversation import detect_missing_fields, process_reply, start_conversation
    from modules.dispatch import create_dispatch, get_ticket_dispatches
    from modules.events import get_ticket_events
    from modules.matching import match_staff
    from modules.recommendation import generate_recommendation_explanation
    from modules.service_tracking import accept_dispatch, start_service, complete_service
    from modules.staff import create_staff, get_staff
    from modules.tickets import (
        TICKET_FIELDS, create_ticket, review_ticket, confirm_priority,
        generate_ticket_advice, confirm_advice,
    )

    config.DEMO_MODE = True
    db.DB_PATH = Path(path)
    ai_logging.LOG_PATH = db.DB_PATH.parent / "ai_calls.json"
    db.init_db()
    with db._connection() as connection:
        connection.execute("CREATE TABLE demo_flow_snapshot (id INTEGER PRIMARY KEY, payload TEXT NOT NULL, is_demo INTEGER NOT NULL DEFAULT 1 CHECK(is_demo = 1))")
        # 仅演示库增加标记；真实库结构、核心业务代码均不变。
        for table in DEMO_TABLES:
            columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            if "is_demo" not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 1 CHECK(is_demo = 1)")

    steps = []

    def step(summary, details):
        steps.append({"number": len(steps) + 1, "title": STEP_TITLES[len(steps)],
                      "summary": summary, "details": json.loads(json.dumps(details, ensure_ascii=False)), "is_demo": 1})

    # 固定演示案例使用显式的六字段模拟输出，不改 parser 接口，也不伪装真实调用。
    values = dict.fromkeys(REQUEST_FIELDS, "")
    values.update(service_type="护理员上门", appointment_time="明天下午两点", address="红塔区山水佳园")
    parsed = ParsedRequest({field: {"value": value, "confidence": 0.95 if value else 0.0}
                            for field, value in values.items()})
    parsed.call_status, parsed.model = "demo", "demo-flow-fixture"
    step("从客户原文提取服务、时间和地址；姓名、电话未提供。此处为预置模拟解析。", dict(parsed))

    ticket = create_ticket(DEMO_REQUEST, parsed)
    ticket_id = ticket["id"]
    step("创建唯一工单，保留原文和解析结果，缺少关键字段进入人工审核。", db.get_ticket(ticket_id))

    missing = detect_missing_fields(ticket)
    step("必需字段检查发现缺少联系电话和客户姓名，不能直接派单。", missing)

    start_conversation(ticket_id)
    # 用两次客户回复展示同一工单的多轮补全；姓名和电话均为明确的虚构资料。
    process_reply(ticket_id, "姓名：演示客户")
    process_reply(ticket_id, "电话：演示电话001")
    filled = db.get_ticket(ticket_id)
    review_ticket(ticket_id, {field: filled[field] for field in TICKET_FIELDS})
    step("两轮补齐姓名、电话；演示运营核对后审核通过，工单编号保持不变。",
         {"ticket": db.get_ticket(ticket_id), "conversation": db.get_conversation_state(ticket_id)})

    confirm_priority(ticket_id, "高", "演示运营核对行动不便与预约时限，确认优先跟进；仅为演示决策。")
    step("展示规则建议，再由演示运营确认高优先级；年龄本身不作为自动高风险结论。", db.get_ticket(ticket_id))

    advised = generate_ticket_advice(ticket_id)
    advice = json.loads(advised["advice_json"])
    confirm_advice(ticket_id, "accepted", format_advice(advice), "演示运营核对后采纳规则模板建议。")
    step("展示待补信息、处理步骤和注意事项；演示运营采纳后保存最终方案。",
         {"advice": advice, "final": db.get_ticket(ticket_id)["advice_final"], "decision": "accepted"})

    person = create_staff("张护理员（演示）", "演示电话002", ["老人护理", "助浴"], "红塔区", is_demo=True)
    ticket = db.get_ticket(ticket_id)
    candidates = match_staff(ticket)
    explanation = generate_recommendation_explanation(ticket, candidates)
    if not candidates or candidates[0]["staff_id"] != person["id"]:
        raise ValueError("演示候选不符合预期。")
    step("按技能、区域与登记状态评分，生成规则说明；预约档期仍需运营确认。",
         {"staff": person, "candidates": candidates, "explanation": explanation})

    dispatch_id = create_dispatch(ticket_id, person["id"], "演示运营", "演示运营已模拟核对服务时间与人员能力，同意派单。")
    step("演示运营确认后创建派单，人员变为忙碌，派单与事件同事务保存。",
         {"dispatch": get_ticket_dispatches(ticket_id)[0], "staff": get_staff(person["id"])})

    accept_dispatch(dispatch_id)
    started = start_service(dispatch_id)
    step("依次登记接单、开始服务，记录各环节时间；不跳过状态机。", started)

    completed = complete_service(dispatch_id, "虚构演示：服务已完成；不代表实际履约记录。")
    step("登记服务完成并释放人员占用，保留全过程事件。工单人工状态仍独立管理。",
         {"dispatch": completed, "staff": get_staff(person["id"])})

    payload = {
        "is_demo": 1, "customer_request": DEMO_REQUEST, "steps": steps,
        "ticket": db.get_ticket(ticket_id), "staff": get_staff(person["id"]),
        "dispatch": completed, "events": get_ticket_events(ticket_id),
    }
    with db._connection() as connection:
        connection.execute("INSERT INTO demo_flow_snapshot (id, payload) VALUES (1, ?)",
                           (json.dumps(payload, ensure_ascii=False, allow_nan=False),))
        for table in DEMO_TABLES:
            if connection.execute(f"SELECT COUNT(*) FROM {table} WHERE is_demo IS NULL OR is_demo <> 1").fetchone()[0]:
                raise ValueError("演示数据标记不完整。")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--build":
        _build_demo(Path(sys.argv[2]))
    else:
        print(json.dumps({"ticket_id": run_demo_flow()["ticket"]["id"], "is_demo": 1}))
