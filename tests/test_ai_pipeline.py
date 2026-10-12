"""AI 成功与降级的完整流程，使用模拟 SDK、临时日志和临时数据库。"""

import json
import os
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from openai import APIError, APITimeoutError
from streamlit.testing.v1 import AppTest

import config
from database import db
from modules import ai_logging, ai_parser
from modules.automation import REPORT_TIMEZONE
from modules.dashboard import get_dashboard_stats
from modules.tickets import create_ticket, review_ticket


class AIPipelineTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self._patch(patch.object(db, "DB_PATH", Path(directory.name) / "autoflow.db"))
        self._patch(patch.object(ai_logging, "LOG_PATH", Path(directory.name) / "logs" / "ai_calls.json"))
        self._patch(patch.object(config, "DEMO_MODE", False))
        self._patch(patch.dict(os.environ, {"OPENAI_API_KEY": "offline-test-key", "OPENAI_MODEL": "gpt-4o-mini"}))
        self._patch(patch.object(ai_parser, "load_dotenv"))
        fixed_now = datetime(2026, 10, 12, 12, tzinfo=REPORT_TIMEZONE)
        for target in ("modules.ai_parser.datetime", "modules.automation.datetime", "modules.dashboard.datetime", "modules.tickets.datetime"):
            clock = self._patch(patch(target, wraps=datetime))
            clock.now.return_value = fixed_now
        self._patch(patch.object(ai_parser.time, "perf_counter", side_effect=[10.0, 10.4]))
        self.sdk = self._patch(patch.object(ai_parser, "OpenAI"))
        self.client = self.sdk.return_value.__enter__.return_value
        self.values = {
            "service_type": "空调清洗", "appointment_time": "周六上午", "address": "示例路1号",
            "customer_name": "演示客户", "phone": "演示电话001", "urgency": "普通",
        }
        self.fields = {field: {"value": value, "confidence": 0.9} for field, value in self.values.items()}
        self.client.responses.create.return_value = SimpleNamespace(
            status="completed", output=[], output_text=json.dumps(self.fields),
            usage=SimpleNamespace(total_tokens=123),
        )

    def _patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def assert_review_fallback(self, result, expected_status):
        self.assertEqual(result, {field: {"value": "", "confidence": 0.0} for field in ai_parser.REQUEST_FIELDS})
        self.assertEqual(result.call_status, expected_status)
        ticket = create_ticket("保留的虚构客户原文", result)
        self.assertEqual(ticket["status"], "人工审核")
        self.assertIs(ticket["review_required"], True)
        self.assertEqual(db.get_ticket(ticket["id"]), ticket)
        return ticket

    def test_success_saves_metrics_and_logs_structured_output(self):
        parsed = ai_parser.parse_customer_request("虚构客户原始需求")
        self.assertEqual(set(parsed), set(ai_parser.REQUEST_FIELDS))
        self.assertEqual(parsed, self.fields)
        ticket = create_ticket("虚构客户原始需求", parsed)
        self.assertEqual(ticket["token_used"], 123)
        self.assertAlmostEqual(ticket["response_time"], 0.4)
        self.assertEqual(ticket["status"], "待确认")
        self.assertEqual(db.get_ticket(ticket["id"]), ticket)
        log = ai_logging.read_ai_calls()[0]
        self.assertTrue({"timestamp", "input", "output", "status", "error"}.issubset(log))
        self.assertEqual(log["timestamp"], "2026-10-12T12:00:00+08:00")
        self.assertEqual(log["output"], self.fields)
        self.assertEqual(log["status"], "success")
        self.assertEqual(log["error"], "")
        self.assertEqual(get_dashboard_stats()["ai_call_count"], 1)
        self.assertAlmostEqual(get_dashboard_stats()["avg_response_time"], 0.4)

    def test_api_error_creates_review_ticket_without_leaking_error_details(self):
        detail = "offline-sensitive-error-detail"
        self.client.responses.create.side_effect = APIError(detail, request=SimpleNamespace(), body=None)
        parsed = ai_parser.parse_customer_request("虚构需求")
        ticket = self.assert_review_fallback(parsed, "api_error")
        self.assertEqual(ticket["token_used"], 0)
        self.assertAlmostEqual(ticket["response_time"], 0.4)
        self.assertNotIn(detail, ai_logging.LOG_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("offline-test-key", ai_logging.LOG_PATH.read_text(encoding="utf-8"))
        self.client.responses.create.assert_called_once()

    def test_invalid_json_falls_back_and_keeps_known_token_usage(self):
        self.client.responses.create.return_value.output_text = "invalid-json-sensitive-output"
        parsed = ai_parser.parse_customer_request("虚构需求")
        ticket = self.assert_review_fallback(parsed, "json_error")
        self.assertEqual(ticket["token_used"], 123)
        self.assertNotIn("invalid-json-sensitive-output", ai_logging.LOG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(len(ai_logging.read_ai_calls()), 1)

    def test_missing_key_creates_review_ticket_without_counting_api_attempt(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            parsed = ai_parser.parse_customer_request("虚构需求")
        ticket = self.assert_review_fallback(parsed, "demo")
        self.assertEqual(ticket["token_used"], 0)
        self.assertIsNone(ticket["response_time"])
        log = ai_logging.read_ai_calls()[0]
        self.assertEqual(log["status"], "demo")
        self.assertIs(log["api_called"], False)
        self.assertEqual(log["token_used"], 0)
        self.assertIsNone(log["response_time"])
        self.sdk.assert_not_called()
        self.assertEqual(get_dashboard_stats()["ai_call_count"], 0)
        self.assertIsNone(get_dashboard_stats()["avg_response_time"])

    def test_logging_failure_does_not_lose_successful_ticket(self):
        with patch.object(ai_parser, "append_ai_call", side_effect=OSError("offline-log-error")):
            parsed = ai_parser.parse_customer_request("虚构需求")
        self.assertTrue(parsed.logging_error)
        ticket = create_ticket("虚构需求", parsed)
        self.assertEqual(ticket["status"], "待确认")
        self.assertEqual(ticket["token_used"], 123)
        self.assertEqual(len(db.get_all_tickets()), 1)
        self.client.responses.create.assert_called_once()

    def test_dashboard_counts_attempt_even_if_database_save_fails(self):
        parsed = ai_parser.parse_customer_request("虚构需求")
        with patch("modules.tickets.save_ticket", side_effect=OSError("offline-db-error")):
            with self.assertRaises(OSError):
                create_ticket("虚构需求", parsed)
        self.assertEqual(get_dashboard_stats()["total_tickets"], 0)
        self.assertEqual(get_dashboard_stats()["ai_call_count"], 1)

    def test_page_timeout_survives_and_manual_review_preserves_metrics(self):
        self.client.responses.create.side_effect = APITimeoutError(request=SimpleNamespace())
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
        self.assertFalse(app.exception)
        self.sdk.assert_not_called()
        app.text_area[0].set_value("请安排空调清洗。")
        next(item for item in app.button if item.label == "创建工单").click().run(timeout=30)
        self.assertFalse(app.exception)
        ticket = json.loads(app.json[1].value)
        self.assertEqual(ticket["status"], "人工审核")
        self.assertTrue(any("降级" in item.value for item in app.warning))
        metrics = {item.label: item.value for item in app.metric}
        self.assertEqual(metrics["AI调用次数"], "1")
        self.assertEqual(metrics["平均响应时间"], "0.40 秒")
        for field, value in self.values.items():
            label = {"service_type": "服务类型", "appointment_time": "预约时间", "address": "服务地址",
                     "customer_name": "客户姓名", "phone": "电话", "urgency": "紧急程度"}[field]
            next(item for item in app.text_input if item.label == label).set_value(value)
        app.checkbox[0].check()
        next(item for item in app.button if item.label == "审核通过并保存").click().run(timeout=30)
        self.assertFalse(app.exception)
        reviewed = db.get_ticket(ticket["id"])
        self.assertEqual(reviewed["status"], "待确认")
        self.assertEqual(reviewed["response_time"], ticket["response_time"])
        self.assertEqual(reviewed["token_used"], ticket["token_used"])
        self.assertEqual(reviewed["confidence_score"], ticket["confidence_score"])
        self.assertEqual(len(ai_logging.read_ai_calls()), 1)
        self.client.responses.create.assert_called_once()
        app.run(timeout=30)
        self.assertFalse(app.exception)
        self.assertEqual(get_dashboard_stats()["ai_call_count"], 1)

    def test_page_keeps_ticket_kpis_when_call_log_is_corrupt(self):
        db.init_db()
        ai_logging.LOG_PATH.parent.mkdir(parents=True)
        ai_logging.LOG_PATH.write_text("corrupt-log", encoding="utf-8")
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
        self.assertFalse(app.exception)
        metrics = {item.label: item.value for item in app.metric}
        self.assertEqual(metrics["工单总数"], "0")
        self.assertEqual(metrics["AI调用次数"], "暂不可用")
        self.assertTrue(any("AI调用统计暂不可用" in item.value for item in app.warning))
        self.sdk.assert_not_called()


if __name__ == "__main__":
    unittest.main()
