"""看板统计与页面刷新检查；使用固定日期和临时数据库。"""

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
from modules import ai_logging, ai_parser, dashboard
from modules.automation import REPORT_TIMEZONE
from modules.tickets import create_ticket, review_ticket


class DashboardTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self._patch(patch.object(db, "DB_PATH", Path(directory.name) / "autoflow.db"))
        self.log_path = Path(directory.name) / "logs.json"
        self._patch(patch.object(ai_logging, "LOG_PATH", self.log_path))
        self._patch(patch.object(config, "DEMO_MODE", True))
        self._patch(patch.dict(os.environ, {"OPENAI_API_KEY": "", "OPENAI_MODEL": "gpt-4o-mini"}))
        self._patch(patch.object(ai_parser, "load_dotenv"))
        fixed_now = datetime(2026, 10, 11, 12, tzinfo=REPORT_TIMEZONE)
        for target in ("modules.dashboard.datetime", "modules.automation.datetime", "modules.tickets.datetime"):
            clock = self._patch(patch(target, wraps=datetime))
            clock.now.return_value = fixed_now
        self.fields = {
            "service_type": "空调清洗", "appointment_time": "周六上午", "address": "示例路1号",
            "customer_name": "演示客户", "phone": "演示电话001", "urgency": "普通",
        }
        self.parser = self._patch(
            patch("modules.ai_parser.parse_customer_request", return_value={
                field: {"value": value, "confidence": 0.95 if value else 0.0}
                for field, value in self.fields.items()
            })
        )

    def _patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def add_ticket(self, ticket_id, status, service="空调清洗", created_time="2026-10-11T10:00:00+08:00"):
        db.save_ticket({
            "id": ticket_id, "customer_input": "虚构需求", "service_type": service,
            "status": status, "created_time": created_time,
        })

    def open_app(self):
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
        self.assertFalse(app.exception)
        return app

    def kpis(self, app):
        return {metric.label: metric.value for metric in app.metric
                if metric.label in ("工单总数", "今日新增", "累计已完成", "异常工单")}

    def test_empty_database_returns_zero_and_empty_distributions(self):
        self.assertEqual(dashboard.get_dashboard_stats(), {
            "date": "2026-10-11", "total_tickets": 0, "today_new_tickets": 0,
            "status_counts": {}, "service_type_counts": {},
            "ai_call_count": 0, "avg_response_time": None, "ai_metrics_error": None,
            "ai_success_count": 0, "ai_success_rate": None, "review_required_count": 0,
        })
        self.parser.assert_not_called()

    def test_ai_metrics_include_successful_and_failed_calls_without_using_ticket_count(self):
        self.add_ticket("saved", "待确认")
        self.log_path.write_text(json.dumps([
            {"api_called": True, "response_time": 0.2, "status": "success"},
            {"api_called": True, "response_time": 0.6, "status": "api_error"},
            {"api_called": False, "response_time": 0.0, "status": "configuration_error"},
            {"api_called": False, "response_time": None, "status": "demo"},
        ]), encoding="utf-8")
        stats = dashboard.get_dashboard_stats()
        self.assertEqual(stats["total_tickets"], 1)
        self.assertEqual(stats["ai_call_count"], 2)
        self.assertAlmostEqual(stats["avg_response_time"], 0.4)
        self.assertEqual(stats["ai_success_count"], 1)
        self.assertEqual(stats["ai_success_rate"], 0.5)
        self.assertIsNone(stats["ai_metrics_error"])
        self.parser.assert_not_called()

    def test_ai_average_is_unavailable_when_calls_have_no_measured_duration(self):
        self.log_path.write_text(json.dumps([
            {"api_called": True, "response_time": None},
            {"api_called": True},
        ]), encoding="utf-8")
        stats = dashboard.get_dashboard_stats()
        self.assertEqual(stats["ai_call_count"], 2)
        self.assertIsNone(stats["avg_response_time"])
        self.assertEqual(stats["ai_success_count"], 0)
        self.assertEqual(stats["ai_success_rate"], 0.0)
        self.assertIsNone(stats["ai_metrics_error"])
        self.parser.assert_not_called()

    def test_bad_ai_log_does_not_break_business_statistics(self):
        self.add_ticket("saved", "待确认")
        self.log_path.write_text("invalid JSON", encoding="utf-8")
        stats = dashboard.get_dashboard_stats()
        self.assertEqual(stats["total_tickets"], 1)
        self.assertEqual(stats["today_new_tickets"], 1)
        self.assertEqual(stats["status_counts"], {"待确认": 1})
        self.assertEqual(stats["service_type_counts"], {"空调清洗": 1})
        self.assertIsNone(stats["ai_call_count"])
        self.assertIsNone(stats["avg_response_time"])
        self.assertIsNone(stats["ai_success_count"])
        self.assertIsNone(stats["ai_success_rate"])
        self.assertEqual(stats["review_required_count"], 0)
        self.assertEqual(stats["ai_metrics_error"], "AI调用日志暂不可读取")
        self.parser.assert_not_called()

    def test_ai_log_read_failure_does_not_break_business_statistics(self):
        self.add_ticket("saved", "已完成")
        with patch.object(dashboard, "get_ai_call_stats", side_effect=OSError("test")):
            stats = dashboard.get_dashboard_stats()
        self.assertEqual(stats["total_tickets"], 1)
        self.assertEqual(stats["status_counts"], {"已完成": 1})
        self.assertIsNone(stats["ai_call_count"])
        self.assertIsNone(stats["avg_response_time"])
        self.assertIsNone(stats["ai_success_count"])
        self.assertIsNone(stats["ai_success_rate"])
        self.assertEqual(stats["review_required_count"], 0)
        self.assertEqual(stats["ai_metrics_error"], "AI调用日志暂不可读取")
        self.parser.assert_not_called()

    def test_review_required_count_uses_current_flags_and_changes_after_review(self):
        parsed = {field: {"value": value, "confidence": 0.85} for field, value in self.fields.items()}
        parsed["phone"]["confidence"] = 0.55
        ticket = create_ticket("虚构待审核工单", parsed)
        self.assertEqual(dashboard.get_dashboard_stats()["review_required_count"], 1)
        reviewed = review_ticket(ticket["id"], self.fields)
        self.assertEqual(reviewed["confidence_score"], ticket["confidence_score"])
        self.assertIs(reviewed["review_required"], False)
        self.assertEqual(dashboard.get_dashboard_stats()["review_required_count"], 0)
        db.update_ticket_status(ticket["id"], "已完成")
        stats = dashboard.get_dashboard_stats()
        self.assertEqual(stats["review_required_count"], 0)
        self.assertEqual(stats["status_counts"], {"已完成": 1})
        self.parser.assert_not_called()

    def test_review_required_count_is_independent_of_history_status_and_ai_score(self):
        parsed = {field: {"value": value, "confidence": 0.55} for field, value in self.fields.items()}
        low_score_snapshot = json.dumps(parsed, ensure_ascii=False)
        for ticket_id, status, review_required, score in (
            ("currently-flagged", "待确认", True, "{}"),
            ("historical-reviewed", "人工审核", False, low_score_snapshot),
        ):
            db.save_ticket({
                "id": ticket_id, "customer_input": "虚构需求", "service_type": "空调清洗",
                "status": status, "created_time": "2026-10-11T10:00:00+08:00",
                "review_required": review_required, "confidence_score": score,
            })
        stats = dashboard.get_dashboard_stats()
        self.assertEqual(stats["total_tickets"], 2)
        self.assertEqual(stats["status_counts"], {"人工审核": 1, "待确认": 1})
        self.assertEqual(stats["review_required_count"], 1)
        self.parser.assert_not_called()

    def test_counts_cover_all_records_and_do_not_change_database(self):
        self.add_ticket("a", "待确认")
        self.add_ticket("b", "已完成", " 保洁 ", "2026-10-10T10:00:00+08:00")
        self.add_ticket("c", "异常", "")
        self.add_ticket("d", "待确认", "  ")
        self.add_ticket("e", "已取消", "保洁")
        before = db.get_all_tickets()
        stats = dashboard.get_dashboard_stats()
        self.assertEqual(stats["total_tickets"], 5)
        self.assertEqual(stats["today_new_tickets"], 4)
        self.assertEqual(stats["status_counts"], {"待确认": 2, "已完成": 1, "异常": 1, "已取消": 1})
        self.assertEqual(stats["service_type_counts"], {"空调清洗": 1, "保洁": 2, "未识别": 2})
        self.assertEqual(sum(stats["status_counts"].values()), stats["total_tickets"])
        self.assertEqual(sum(stats["service_type_counts"].values()), stats["total_tickets"])
        self.assertEqual(db.get_all_tickets(), before)
        self.parser.assert_not_called()

    def test_today_uses_beijing_boundaries_and_handles_legacy_local_time(self):
        timestamps = (
            "2026-10-10T15:59:59+00:00", "2026-10-10T16:00:00+00:00",
            "2026-10-11T15:59:59+00:00", "2026-10-11T16:00:00+00:00",
            "2026-10-11T00:00:00",
        )
        for index, created_time in enumerate(timestamps):
            self.add_ticket(str(index), "待确认", created_time=created_time)
        stats = dashboard.get_dashboard_stats()
        self.assertEqual(stats["total_tickets"], 5)
        self.assertEqual(stats["today_new_tickets"], 3)

    def test_database_is_read_once_for_a_consistent_statistics_snapshot(self):
        self.add_ticket("one", "服务中")
        with patch.object(dashboard, "get_all_tickets", wraps=db.get_all_tickets) as reader:
            stats = dashboard.get_dashboard_stats()
            reader.assert_called_once_with()
        self.assertEqual(stats["status_counts"], {"服务中": 1})

    def test_invalid_creation_time_is_not_silently_ignored(self):
        self.add_ticket("bad", "异常", created_time="bad-date")
        with self.assertRaises(ValueError):
            dashboard.get_dashboard_stats()

    def test_database_error_propagates(self):
        with patch.object(dashboard, "get_all_tickets", side_effect=sqlite3.OperationalError("test")):
            with self.assertRaises(sqlite3.OperationalError):
                dashboard.get_dashboard_stats()

    def test_empty_page_shows_zero_kpis_without_charts(self):
        app = self.open_app()
        self.assertEqual(self.kpis(app), {
            "工单总数": "0", "今日新增": "0", "累计已完成": "0", "异常工单": "0",
        })
        self.assertFalse(app.get("vega_lite_chart"))
        self.parser.assert_not_called()

    def test_kpis_and_charts_refresh_after_creation_and_status_change(self):
        app = self.open_app()
        app.text_area[0].set_value("请安排空调清洗。")
        next(item for item in app.button if item.label == "创建工单").click().run(timeout=30)
        self.assertFalse(app.exception)
        self.assertEqual(self.kpis(app), {
            "工单总数": "1", "今日新增": "1", "累计已完成": "0", "异常工单": "0",
        })
        self.assertEqual(len(app.get("vega_lite_chart")), 3)
        for status, completed, abnormal in (("已完成", "1", "0"), ("异常", "0", "1")):
            next(item for item in app.selectbox if item.label == "修改状态").set_value(status)
            next(item for item in app.button if item.label == "保存状态").click().run(timeout=30)
            self.assertFalse(app.exception)
            self.assertEqual(self.kpis(app), {
                "工单总数": "1", "今日新增": "1", "累计已完成": completed, "异常工单": abnormal,
            })
            self.assertEqual(dashboard.get_dashboard_stats()["status_counts"], {status: 1})
        fresh = self.open_app()
        self.assertEqual(self.kpis(fresh), self.kpis(app))
        self.assertEqual(len(db.get_all_tickets()), 1)
        self.assertEqual(self.parser.call_count, 1)

    def test_page_shows_dashboard_errors_without_crashing(self):
        with patch.object(dashboard, "get_all_tickets", side_effect=sqlite3.OperationalError("test")):
            app = self.open_app()
            self.assertTrue(any("运营看板读取失败" in item.value for item in app.error))
        with patch.object(dashboard, "get_all_tickets", return_value=[{"created_time": "bad-date"}]):
            app = self.open_app()
            self.assertTrue(any("运营看板生成失败" in item.value for item in app.error))
        self.parser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
