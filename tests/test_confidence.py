"""置信度阈值、持久化与人工审核流程的离线回归测试。"""

import json
import os
import sqlite3
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import config
from database import db
from modules import ai_logging, ai_parser
from modules.automation import REPORT_TIMEZONE, generate_daily_report
from modules.dashboard import get_dashboard_stats
from modules.rules import CRITICAL_FIELDS, check_confidence, get_confidence_score
from modules.tickets import create_ticket, get_saved_parse_result, review_ticket


class ConfidenceTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self._patch(patch.object(db, "DB_PATH", Path(directory.name) / "autoflow.db"))
        self._patch(patch.object(ai_logging, "LOG_PATH", Path(directory.name) / "ai_calls.json"))
        self._patch(patch.object(config, "DEMO_MODE", True))
        self._patch(patch.dict(os.environ, {"OPENAI_API_KEY": "", "OPENAI_MODEL": "gpt-4o-mini"}))
        self._patch(patch.object(ai_parser, "load_dotenv"))
        fixed_now = datetime(2026, 10, 11, 12, tzinfo=REPORT_TIMEZONE)
        for target in ("modules.automation.datetime", "modules.dashboard.datetime", "modules.tickets.datetime"):
            clock = self._patch(patch(target, wraps=datetime))
            clock.now.return_value = fixed_now
        self.values = {
            "service_type": "空调清洗", "appointment_time": "周六上午", "address": "示例路1号",
            "customer_name": "演示客户", "phone": "演示电话001", "urgency": "普通",
        }
        self.parsed = {field: {"value": value, "confidence": 0.85} for field, value in self.values.items()}
        self.parser = self._patch(patch("modules.ai_parser.parse_customer_request", return_value=self.parsed))

    def _patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def open_app(self):
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
        self.assertFalse(app.exception)
        return app

    def create_from_page(self, app):
        app.text_area[0].set_value("请周六上午安排空调清洗，地址示例路1号，电话演示电话001。")
        next(item for item in app.button if item.label == "创建工单").click().run(timeout=30)
        self.assertFalse(app.exception)
        return json.loads(app.json[1].value)

    def submit_review(self, app):
        next(item for item in app.button if item.label == "审核通过并保存").click().run(timeout=30)
        self.assertFalse(app.exception)

    def test_high_confidence_returns_pending_confirmation(self):
        self.assertEqual(check_confidence(self.parsed), "待确认")
        ticket = create_ticket("高分测试", self.parsed)
        self.assertEqual(ticket["status"], "待确认")
        self.assertIs(ticket["review_required"], False)
        self.assertEqual(get_confidence_score(self.parsed), 0.85)

    def test_each_low_critical_field_requires_review_even_when_others_are_high(self):
        for field in CRITICAL_FIELDS:
            with self.subTest(field=field):
                parsed = {key: {**item, "confidence": 0.99} for key, item in self.parsed.items()}
                parsed[field]["confidence"] = 0.55
                self.assertEqual(check_confidence(parsed), "人工审核")
                self.assertEqual(get_confidence_score(parsed), 0.55)
                ticket = create_ticket("低分测试", parsed)
                self.assertEqual(ticket["status"], "人工审核")
                self.assertIs(ticket["review_required"], True)

    def test_threshold_boundary_and_non_critical_fields(self):
        for field in CRITICAL_FIELDS:
            self.parsed[field]["confidence"] = 0.7
        self.parsed["customer_name"]["confidence"] = 0
        self.parsed["urgency"]["confidence"] = 0
        self.assertEqual(check_confidence(self.parsed), "待确认")
        self.parsed["phone"]["confidence"] = 0.6999
        self.assertEqual(check_confidence(self.parsed), "人工审核")

    def test_missing_empty_and_invalid_critical_confidence_require_review(self):
        for bad in (None, True, "0.9", -1, 2, float("nan"), float("inf"), 10**400):
            with self.subTest(confidence=repr(bad)):
                parsed = {**self.parsed, "phone": {"value": "演示电话001", "confidence": bad}}
                self.assertEqual(check_confidence(parsed), "人工审核")
        self.assertEqual(check_confidence({}), "人工审核")
        self.assertEqual(check_confidence({**self.parsed, "address": {"value": " ", "confidence": 0.99}}), "人工审核")

    def test_original_ai_result_and_boolean_review_flag_round_trip(self):
        self.parsed["address"]["confidence"] = 0.55
        ticket = create_ticket("持久化测试", self.parsed)
        stored = db.get_ticket(ticket["id"])
        self.assertEqual(stored, ticket)
        self.assertEqual(get_saved_parse_result(stored), self.parsed)
        self.assertIs(stored["review_required"], True)

    def test_manual_review_updates_values_and_keeps_original_ai_snapshot(self):
        self.parsed["phone"]["confidence"] = 0.55
        original = create_ticket("审核测试", self.parsed)
        reviewed_values = {**self.values, "phone": "演示电话100", "address": "修正后的测试地址"}
        reviewed = review_ticket(original["id"], reviewed_values)
        self.assertEqual(reviewed["status"], "待确认")
        self.assertIs(reviewed["review_required"], False)
        self.assertEqual(db.get_ticket(original["id"]), reviewed)
        self.assertEqual(reviewed["confidence_score"], original["confidence_score"])
        self.assertEqual(reviewed["created_time"], original["created_time"])
        self.assertEqual(get_saved_parse_result(reviewed)["phone"]["value"], self.values["phone"])
        self.assertEqual(reviewed["phone"], reviewed_values["phone"])
        self.assertIsNone(review_ticket(original["id"], reviewed_values))
        self.parser.assert_not_called()

    def test_manual_review_of_missing_key_information_waits_for_supplement(self):
        self.parsed["phone"]["confidence"] = 0.55
        for field in CRITICAL_FIELDS:
            with self.subTest(field=field):
                ticket = create_ticket("未填齐测试", self.parsed)
                reviewed = review_ticket(ticket["id"], {**self.values, field: ""})
                self.assertEqual(reviewed["status"], "待补充信息")
                self.assertIs(reviewed["review_required"], False)

    def test_review_cannot_be_bypassed_using_plain_status_update(self):
        self.parsed["phone"]["confidence"] = 0.55
        ticket = create_ticket("审核门禁测试", self.parsed)
        for status in ("待确认", "待派单", "服务中", "已完成"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                db.update_ticket_status(ticket["id"], status)
        self.assertEqual(db.get_ticket(ticket["id"]), ticket)

    def test_legacy_flat_input_still_uses_original_missing_information_rule(self):
        ticket = create_ticket("旧输入测试", {**self.values, "phone": ""})
        self.assertEqual(ticket["status"], "待补充信息")
        self.assertIs(ticket["review_required"], False)
        self.assertEqual(get_saved_parse_result(ticket), {})

    def test_reviews_are_included_in_daily_report_and_dashboard(self):
        self.parsed["phone"]["confidence"] = 0.55
        ticket = create_ticket("统计测试", self.parsed)
        self.assertEqual(generate_daily_report()["today"]["pending_review"], 1)
        self.assertEqual(generate_daily_report()["today"]["pending"], 1)
        self.assertEqual(get_dashboard_stats()["status_counts"], {"人工审核": 1})
        review_ticket(ticket["id"], self.values)
        self.assertEqual(generate_daily_report()["today"]["pending_review"], 0)
        self.assertEqual(get_dashboard_stats()["status_counts"], {"待确认": 1})

    def test_page_displays_high_confidence_without_pending_review(self):
        app = self.open_app()
        ticket = self.create_from_page(app)
        self.assertIs(ticket["review_required"], False)
        self.assertTrue(any("85%" in item.value for item in app.markdown))
        self.assertTrue(any("无需人工审核" in item.value for item in app.markdown))
        self.assertFalse(any(item.label == "审核通过并保存" for item in app.button))

    def test_page_shows_raw_score_near_the_rounding_boundary(self):
        self.parsed["phone"]["confidence"] = 0.699
        app = self.open_app()
        ticket = self.create_from_page(app)
        self.assertEqual(ticket["status"], "人工审核")
        self.assertTrue(any("原始分数：0.699" in item.value for item in app.markdown))

    def test_page_requires_confirmation_and_persists_reviewed_fields(self):
        self.parsed["phone"]["confidence"] = 0.55
        app = self.open_app()
        original = self.create_from_page(app)
        self.assertIs(original["review_required"], True)
        self.assertTrue(any("55%" in item.value for item in app.markdown))
        self.assertTrue(any("需要人工审核" in item.value for item in app.markdown))
        self.assertFalse(any(item.label == "保存状态" for item in app.button))
        self.submit_review(app)
        self.assertTrue(any("勾选确认" in item.value for item in app.warning))
        self.assertIs(db.get_ticket(original["id"])["review_required"], True)
        next(item for item in app.text_input if item.label == "电话").set_value("演示电话100")
        app.checkbox[0].check()
        self.submit_review(app)
        reviewed = db.get_ticket(original["id"])
        self.assertEqual(reviewed["phone"], "演示电话100")
        self.assertEqual(reviewed["status"], "待确认")
        self.assertIs(reviewed["review_required"], False)
        self.assertEqual(reviewed["confidence_score"], original["confidence_score"])
        self.assertEqual(json.loads(app.json[1].value), reviewed)
        self.assertTrue(any("已完成人工审核" in item.value for item in app.markdown))
        fresh = self.open_app()
        self.assertFalse(fresh.checkbox)
        self.assertTrue(any("已完成人工审核" in item.value for item in fresh.markdown))
        self.assertEqual(len(db.get_all_tickets()), 1)
        self.assertEqual(self.parser.call_count, 1)

    def test_page_review_write_failure_leaves_review_pending(self):
        self.parsed["phone"]["confidence"] = 0.55
        app = self.open_app()
        ticket = self.create_from_page(app)
        app.checkbox[0].check()
        with patch("modules.tickets.update_ticket_after_review", side_effect=sqlite3.OperationalError("test")):
            self.submit_review(app)
        self.assertTrue(any("审核保存失败" in item.value for item in app.error))
        self.assertEqual(db.get_ticket(ticket["id"]), ticket)
        self.assertEqual(self.parser.call_count, 1)


if __name__ == "__main__":
    unittest.main()
