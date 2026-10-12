"""演示准备与真实模式页面的离线检查，不使用实际密钥或业务数据。"""

import json
import os
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from dotenv import load_dotenv
from streamlit.testing.v1 import AppTest

import config
from database import db
from modules import ai_logging, ai_parser
from modules.automation import REPORT_TIMEZONE
from modules.dashboard import get_dashboard_stats


class DemoTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self._patch(patch.object(db, "DB_PATH", self.directory / "autoflow.db"))
        self._patch(patch.object(ai_logging, "LOG_PATH", self.directory / "ai_calls.json"))
        self._patch(patch.object(config, "DEMO_MODE", True))
        self._patch(patch.dict(os.environ, {"OPENAI_API_KEY": "", "OPENAI_MODEL": "gpt-4o-mini"}))
        self.dotenv = self._patch(patch.object(ai_parser, "load_dotenv"))
        self.sdk = self._patch(patch.object(ai_parser, "OpenAI"))
        self.parser = self._patch(patch.object(ai_parser, "parse_customer_request", wraps=ai_parser.parse_customer_request))
        fixed_now = datetime(2026, 10, 12, 12, tzinfo=REPORT_TIMEZONE)
        for target in ("modules.ai_parser.datetime", "modules.automation.datetime", "modules.dashboard.datetime", "modules.tickets.datetime"):
            clock = self._patch(patch(target, wraps=datetime))
            clock.now.return_value = fixed_now

    def _patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def open_app(self):
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
        self.assertFalse(app.exception)
        return app

    def click(self, app, label):
        next(button for button in app.button if button.label == label).click().run(timeout=30)
        self.assertFalse(app.exception)

    def test_startup_without_key_uses_demo_even_when_switch_is_false(self):
        config.DEMO_MODE = False
        app = self.open_app()
        self.assertTrue(any(item.value == "当前为演示模式，未连接真实AI。" for item in app.info))
        self.assertTrue(any("AI状态：○ 模拟模式" in item.value for item in app.markdown))
        self.parser.assert_not_called()
        self.sdk.assert_not_called()
        self.assertEqual(db.get_all_tickets(), [])
        self.assertFalse(ai_logging.LOG_PATH.exists())

    def test_explicit_demo_with_key_still_displays_simulation_and_calls_no_sdk(self):
        os.environ["OPENAI_API_KEY"] = "offline-test-key"
        app = self.open_app()
        self.assertTrue(any("AI状态：○ 模拟模式" in item.value for item in app.markdown))
        self.assertTrue(any("使用模拟解析" in item.value for item in app.info))
        self.assertFalse(any("未连接真实AI" in item.value for item in app.info))
        self.sdk.assert_not_called()
        self.parser.assert_not_called()

    def test_real_route_is_shown_as_configured_without_claiming_connectivity(self):
        config.DEMO_MODE = False
        os.environ["OPENAI_API_KEY"] = "offline-test-key"
        app = self.open_app()
        self.assertTrue(any("AI状态：○ GPT实时解析" in item.value for item in app.markdown))
        self.assertTrue(any("API 连通性以实际调用结果为准" in item.value for item in app.caption))
        self.assertFalse(any("未连接真实AI" in item.value for item in app.info))
        self.sdk.assert_not_called()
        self.parser.assert_not_called()
        self.assertFalse(ai_logging.LOG_PATH.exists())

    def test_case_button_only_fills_input_even_after_repeated_clicks_and_edits(self):
        app = self.open_app()
        for _ in range(2):
            self.click(app, "填入 Demo 案例")
            self.assertEqual(app.text_area[0].value, config.DEMO_CUSTOMER_REQUEST)
        app.text_area[0].set_value("运营人员手动输入的需求").run(timeout=30)
        self.click(app, "填入 Demo 案例")
        self.assertEqual(app.text_area[0].value, config.DEMO_CUSTOMER_REQUEST)
        app.run(timeout=30)
        self.assertFalse(app.exception)
        self.parser.assert_not_called()
        self.sdk.assert_not_called()
        self.assertEqual(db.get_all_tickets(), [])
        self.assertFalse(ai_logging.LOG_PATH.exists())

    def test_demo_case_creation_and_review_do_not_count_or_repeat_ai_calls(self):
        app = self.open_app()
        self.click(app, "填入 Demo 案例")
        self.click(app, "创建工单")
        ticket = json.loads(app.json[1].value)
        self.assertEqual(ticket["customer_input"], config.DEMO_CUSTOMER_REQUEST)
        self.assertEqual(ticket["service_type"], "上门护理")
        self.assertEqual(ticket["appointment_time"], "明天下午两点")
        self.assertEqual(ticket["address"], "示例区演示小区")
        self.assertEqual(ticket["phone"], "")
        self.assertEqual(ticket["status"], "人工审核")
        self.assertTrue(ticket["review_required"])
        self.assertEqual(ticket["token_used"], 0)
        self.assertIsNone(ticket["response_time"])
        self.assertFalse(any(button.label == "保存状态" for button in app.button))
        self.assertFalse(any("AI 解析已降级" in item.value for item in app.warning))
        metrics = {metric.label: metric.value for metric in app.metric}
        self.assertEqual(metrics["AI调用次数"], "0")
        self.assertEqual(metrics["平均响应时间"], "暂无测量")
        self.assertEqual(ai_logging.read_ai_calls()[0]["status"], "demo")
        self.assertFalse(ai_logging.read_ai_calls()[0]["api_called"])
        next(item for item in app.text_input if item.label == "电话").set_value("演示电话002")
        app.checkbox[0].check()
        self.click(app, "审核通过并保存")
        self.assertEqual(db.get_ticket(ticket["id"])["status"], "待确认")
        app.run(timeout=30)
        self.assertFalse(app.exception)
        self.assertEqual(len(db.get_all_tickets()), 1)
        self.assertEqual(len(ai_logging.read_ai_calls()), 1)
        self.parser.assert_called_once_with(config.DEMO_CUSTOMER_REQUEST)
        self.sdk.assert_not_called()

    def test_unrecognized_demo_input_is_preserved_for_review_without_invented_values(self):
        app = self.open_app()
        original = "请安排其他服务，地址和电话稍后补充。"
        app.text_area[0].set_value(original)
        self.click(app, "创建工单")
        ticket = db.get_all_tickets()[0]
        self.assertEqual(ticket["customer_input"], original)
        self.assertEqual(ticket["status"], "人工审核")
        fields = json.loads(ticket["confidence_score"])
        self.assertEqual(fields, {field: {"value": "", "confidence": 0.0} for field in ai_parser.REQUEST_FIELDS})
        self.assertEqual(get_dashboard_stats()["ai_call_count"], 0)
        self.sdk.assert_not_called()

    def test_configuration_read_error_keeps_page_and_manual_ticket_flow_usable(self):
        self.dotenv.side_effect = OSError("offline-private-config-detail")
        app = self.open_app()
        self.assertTrue(any("配置读取失败" in item.value for item in app.warning))
        self.click(app, "填入 Demo 案例")
        self.click(app, "创建工单")
        ticket = db.get_all_tickets()[0]
        self.assertEqual(ticket["status"], "人工审核")
        self.assertEqual(ai_logging.read_ai_calls()[0]["status"], "unexpected_error")
        self.assertNotIn("offline-private-config-detail", ai_logging.LOG_PATH.read_text(encoding="utf-8"))
        self.sdk.assert_not_called()

    def test_real_page_displays_metrics_and_logs_model_and_event_timestamps(self):
        config.DEMO_MODE = False
        os.environ["OPENAI_API_KEY"] = "offline-test-key"
        client = self.sdk.return_value.__enter__.return_value
        values = {
            "service_type": "空调清洗", "appointment_time": "周六上午", "address": "示例路1号",
            "customer_name": "虚构客户", "phone": "演示电话002", "urgency": "普通",
        }
        fields = {field: {"value": value, "confidence": 0.9} for field, value in values.items()}
        client.responses.create.return_value = SimpleNamespace(
            status="completed", output=[], output_text=json.dumps(fields),
            model="offline-model-snapshot", usage=SimpleNamespace(total_tokens=123),
        )
        app = self.open_app()
        app.text_area[0].set_value("虚构客户需求")
        with patch.object(ai_parser.time, "perf_counter", side_effect=[10.0, 10.5]):
            self.click(app, "创建工单")
        ticket = db.get_all_tickets()[0]
        self.assertEqual(ticket["status"], "待确认")
        self.assertEqual(ticket["token_used"], 123)
        self.assertEqual(ticket["response_time"], 0.5)
        self.assertTrue(any("offline-model-snapshot" in item.value and "123" in item.value for item in app.caption))
        entry = ai_logging.read_ai_calls()[0]
        self.assertEqual(entry["model"], "offline-model-snapshot")
        self.assertEqual(entry["request_time"], "2026-10-12T12:00:00+08:00")
        self.assertEqual(entry["response_timestamp"], "2026-10-12T12:00:00+08:00")
        self.assertEqual(set(json.loads(ticket["confidence_score"])), set(ai_parser.REQUEST_FIELDS))
        self.assertNotIn("offline-test-key", json.dumps(entry))
        self.assertEqual(get_dashboard_stats()["ai_call_count"], 1)
        app.run(timeout=30)
        client.responses.create.assert_called_once()
        self.assertEqual(len(db.get_all_tickets()), 1)

    def test_dotenv_reading_and_environment_precedence_use_only_local_fake_credentials(self):
        dotenv_path = self.directory / ".env"
        dotenv_path.write_text("OPENAI_API_KEY=offline-dotenv-key\nOPENAI_MODEL=offline-file-model\n", encoding="utf-8")
        root_env = Path(ai_parser.__file__).resolve().parents[1] / ".env"
        def read_test_file(path, **kwargs):
            self.assertEqual(path, root_env)
            self.assertFalse(kwargs["override"])
            return load_dotenv(dotenv_path, **kwargs)
        self.dotenv.side_effect = read_test_file
        config.DEMO_MODE = False
        os.environ.pop("OPENAI_API_KEY")
        os.environ.pop("OPENAI_MODEL")
        status = ai_parser.get_ai_status()
        self.assertTrue(status["api_key_configured"])
        self.assertFalse(status["demo_mode"])
        self.assertEqual(status["model"], "offline-file-model")
        self.assertEqual(os.environ["OPENAI_API_KEY"], "offline-dotenv-key")
        os.environ["OPENAI_API_KEY"] = "offline-environment-key"
        os.environ["OPENAI_MODEL"] = "offline-environment-model"
        status = ai_parser.get_ai_status()
        self.assertEqual(os.environ["OPENAI_API_KEY"], "offline-environment-key")
        self.assertEqual(status["model"], "offline-environment-model")
        self.assertNotIn("offline-environment-key", json.dumps(status))
        self.sdk.assert_not_called()


if __name__ == "__main__":
    unittest.main()
