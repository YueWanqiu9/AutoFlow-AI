"""面试展示页面与一键演示回归，隔离本地工单、日志和真实 AI。"""

import json
import os
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import config
from database import db
from modules import ai_logging, ai_parser
from modules.automation import REPORT_TIMEZONE


class InterviewDemoTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.root = Path(__file__).resolve().parents[1]
        self._patch(patch.object(db, "DB_PATH", self.directory / "autoflow.db"))
        self._patch(patch.object(ai_logging, "LOG_PATH", self.directory / "ai_calls.json"))
        self._patch(patch.object(config, "DEMO_MODE", True))
        self._patch(patch.dict(os.environ, {"OPENAI_API_KEY": "", "OPENAI_MODEL": "gpt-4o-mini"}))
        self._patch(patch.object(ai_parser, "load_dotenv"))
        self.sdk = self._patch(patch.object(ai_parser, "OpenAI"))
        self.parser = self._patch(patch.object(ai_parser, "parse_customer_request", wraps=ai_parser.parse_customer_request))
        self.demo_parser = self._patch(patch.object(ai_parser, "parse_demo_request", wraps=ai_parser.parse_demo_request))
        fixed_now = datetime(2026, 10, 12, 12, tzinfo=REPORT_TIMEZONE)
        for target in ("modules.ai_parser.datetime", "modules.automation.datetime", "modules.dashboard.datetime", "modules.tickets.datetime"):
            clock = self._patch(patch(target, wraps=datetime))
            clock.now.return_value = fixed_now

    def _patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def open_app(self):
        app = AppTest.from_file(str(self.root / "app.py")).run(timeout=30)
        self.assertFalse(app.exception)
        return app

    def click(self, app, label):
        next(button for button in app.button if button.label == label).click().run(timeout=30)
        self.assertFalse(app.exception)

    def kpis(self, app):
        return {item.label: item.value for item in app.metric}

    def test_home_has_project_title_subtitle_and_all_five_modules(self):
        app = self.open_app()
        self.assertEqual(app.title[0].value, "AutoFlow AI")
        content = "\n".join(item.value for item in app.markdown)
        for label in ("智能服务工单与运营自动化系统", "AI需求解析", "工单自动生成", "业务规则判断", "人工审核机制", "运营数据分析"):
            self.assertIn(label, content)
        self.assertEqual(self.kpis(app)["AI解析成功率"], "暂无真实调用")
        self.assertEqual(self.kpis(app)["人工审核数量"], "0")
        self.sdk.assert_not_called()

    def test_one_click_executes_and_displays_all_five_steps_once(self):
        app = self.open_app()
        self.click(app, "一键演示")
        headings = [item.value for item in app.subheader]
        for label in ("步骤1：AI解析结果", "步骤2：字段置信度", "步骤3：规则判断", "步骤4：生成工单", "步骤5：进入审核/确认"):
            self.assertIn(label, headings)
        self.assertEqual(app.text_area[0].value, config.DEMO_CUSTOMER_REQUEST)
        parsed = json.loads(app.json[0].value)
        ticket = json.loads(app.json[1].value)
        self.assertEqual(parsed["address"]["value"], "示例区演示小区")
        self.assertEqual(parsed["phone"], {"value": "", "confidence": 0.0})
        self.assertEqual(len(app.table[0].value), 6)
        self.assertEqual(ticket["status"], "人工审核")
        self.assertTrue(ticket["review_required"])
        self.assertEqual(db.get_ticket(ticket["id"]), ticket)
        self.assertEqual(self.kpis(app)["工单总数"], "1")
        self.assertEqual(self.kpis(app)["今日新增"], "1")
        self.assertEqual(self.kpis(app)["人工审核数量"], "1")
        self.assertEqual(self.kpis(app)["AI解析成功率"], "暂无真实调用")
        self.assertEqual(self.kpis(app)["AI调用次数"], "0")
        self.assertEqual(len(ai_logging.read_ai_calls()), 1)
        self.demo_parser.assert_called_once_with(config.DEMO_CUSTOMER_REQUEST)
        self.parser.assert_not_called()
        self.sdk.assert_not_called()

    def test_one_click_remains_simulated_when_real_route_and_key_are_configured(self):
        config.DEMO_MODE = False
        os.environ["OPENAI_API_KEY"] = "offline-test-key"
        app = self.open_app()
        self.click(app, "一键演示")
        log = ai_logging.read_ai_calls()[0]
        self.assertEqual(log["status"], "demo")
        self.assertEqual(log["model"], "demo-parser")
        self.assertFalse(log["api_called"])
        self.assertEqual(log["token_used"], 0)
        self.assertIsNone(log["response_time"])
        self.assertTrue(any("未调用真实 AI" in item.value for item in app.caption))
        self.assertNotIn("offline-test-key", json.dumps(log))
        self.parser.assert_not_called()
        self.sdk.assert_not_called()

    def test_rerun_does_not_repeat_one_click_ticket_or_log_creation(self):
        app = self.open_app()
        self.click(app, "一键演示")
        ticket = db.get_all_tickets()[0]
        for _ in range(2):
            app.run(timeout=30)
            self.assertFalse(app.exception)
        self.assertEqual(db.get_all_tickets(), [ticket])
        self.assertEqual(len(ai_logging.read_ai_calls()), 1)
        self.demo_parser.assert_called_once()
        self.sdk.assert_not_called()

    def test_new_intentional_demo_click_creates_new_ticket_and_selects_it(self):
        app = self.open_app()
        self.click(app, "一键演示")
        first = db.get_all_tickets()[0]
        self.click(app, "一键演示")
        tickets = db.get_all_tickets()
        self.assertEqual(len(tickets), 2)
        self.assertNotEqual(tickets[0]["id"], first["id"])
        self.assertEqual(next(item for item in app.selectbox if item.label == "选择工单").value, tickets[0]["id"])
        self.assertEqual(len(ai_logging.read_ai_calls()), 2)
        self.assertEqual(self.demo_parser.call_count, 2)

    def test_review_updates_step_five_and_current_review_count_without_new_parse(self):
        app = self.open_app()
        self.click(app, "一键演示")
        ticket = db.get_all_tickets()[0]
        next(item for item in app.text_input if item.label == "电话").set_value("演示电话002")
        app.checkbox[0].check()
        self.click(app, "审核通过并保存")
        saved = db.get_ticket(ticket["id"])
        self.assertEqual(saved["status"], "待确认")
        self.assertFalse(saved["review_required"])
        self.assertEqual(saved["confidence_score"], ticket["confidence_score"])
        self.assertEqual(json.loads(app.json[1].value), saved)
        self.assertEqual(self.kpis(app)["人工审核数量"], "0")
        self.assertTrue(any("初始规则判断：人工审核" in item.value for item in app.markdown))
        self.assertTrue(any("人工审核已完成" in item.value for item in app.success))
        next(item for item in app.selectbox if item.label == "修改状态").select("已完成")
        self.click(app, "保存状态")
        self.assertEqual(db.get_ticket(ticket["id"])["status"], "已完成")
        self.assertEqual(self.kpis(app)["累计已完成"], "1")
        self.assertEqual(len(db.get_all_tickets()), 1)
        self.assertEqual(len(ai_logging.read_ai_calls()), 1)
        self.demo_parser.assert_called_once()
        self.sdk.assert_not_called()

    def test_failed_demo_database_save_has_no_completed_step_or_automatic_retry(self):
        app = self.open_app()
        with patch("modules.tickets.save_ticket", side_effect=OSError("offline-write-error")):
            self.click(app, "一键演示")
        self.assertTrue(any("工单保存失败" in item.value for item in app.error))
        self.assertFalse(any(item.value == "步骤4：生成工单" for item in app.subheader))
        self.assertEqual(db.get_all_tickets(), [])
        self.assertEqual(len(ai_logging.read_ai_calls()), 1)
        app.run(timeout=30)
        self.assertFalse(app.exception)
        self.assertEqual(db.get_all_tickets(), [])
        self.demo_parser.assert_called_once()

    def test_demo_logging_failure_keeps_saved_ticket_and_five_step_display(self):
        app = self.open_app()
        with patch.object(ai_parser, "append_ai_call", side_effect=OSError("offline-log-error")):
            self.click(app, "一键演示")
        self.assertEqual(len(db.get_all_tickets()), 1)
        self.assertTrue(any("调用日志写入失败" in item.value for item in app.warning))
        self.assertTrue(any(item.value == "步骤5：进入审核/确认" for item in app.subheader))
        self.assertEqual(self.kpis(app)["人工审核数量"], "1")
        self.demo_parser.assert_called_once()
        self.sdk.assert_not_called()

    def test_dashboard_success_rate_excludes_demo_and_keeps_real_measurements(self):
        ai_logging.append_ai_call({"status": "success", "api_called": True, "response_time": 0.2})
        ai_logging.append_ai_call({"status": "json_error", "api_called": True, "response_time": 0.6})
        app = self.open_app()
        self.assertEqual(self.kpis(app)["AI解析成功率"], "50%")
        self.click(app, "一键演示")
        metrics = self.kpis(app)
        self.assertEqual(metrics["AI解析成功率"], "50%")
        self.assertEqual(metrics["AI调用次数"], "2")
        self.assertEqual(metrics["平均响应时间"], "0.40 秒")
        self.sdk.assert_not_called()

    def test_bad_log_keeps_review_metrics_and_shows_success_rate_unavailable(self):
        ai_logging.LOG_PATH.write_text("corrupt-log", encoding="utf-8")
        app = self.open_app()
        self.click(app, "一键演示")
        metrics = self.kpis(app)
        self.assertEqual(metrics["AI解析成功率"], "暂不可用")
        self.assertEqual(metrics["人工审核数量"], "1")
        self.assertEqual(metrics["工单总数"], "1")
        self.assertEqual(ai_logging.LOG_PATH.read_text(encoding="utf-8"), "corrupt-log")
        self.sdk.assert_not_called()

    def test_project_intro_page_explains_architecture_without_business_side_effects(self):
        app = AppTest.from_file(str(self.root / "pages" / "项目介绍.py")).run(timeout=30)
        self.assertFalse(app.exception)
        self.assertEqual(app.title[0].value, "项目介绍")
        headings = [item.value for item in app.subheader]
        for label in ("业务背景", "解决方案", "技术架构"):
            self.assertIn(label, headings)
        architecture = app.code[0].value
        stages = ("用户输入", "AI解析", "规则引擎", "SQLite", "运营Dashboard")
        self.assertEqual([architecture.index(stage) for stage in stages], sorted(architecture.index(stage) for stage in stages))
        self.assertFalse(db.DB_PATH.exists())
        self.assertFalse(ai_logging.LOG_PATH.exists())
        self.parser.assert_not_called()
        self.demo_parser.assert_not_called()
        self.sdk.assert_not_called()


if __name__ == "__main__":
    unittest.main()
