"""运营统计、演示数据事务和看板入口，全部使用临时业务库。"""

import json
import os
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import config
from database import db, seed
from modules import ai_logging, ai_parser, analytics
from modules.automation import REPORT_TIMEZONE
from modules.tickets import create_ticket, review_ticket


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self._patch(patch.object(db, "DB_PATH", Path(directory.name) / "autoflow.db"))
        self._patch(patch.object(ai_logging, "LOG_PATH", Path(directory.name) / "ai_calls.json"))
        self._patch(patch.object(config, "DEMO_MODE", True))
        self._patch(patch.dict(os.environ, {"OPENAI_API_KEY": ""}))
        self._patch(patch.object(ai_parser, "load_dotenv"))
        self.sdk = self._patch(patch.object(ai_parser, "OpenAI"))
        self.now = datetime(2026, 10, 12, 12, tzinfo=REPORT_TIMEZONE)
        for target in ("modules.analytics.datetime", "database.seed.datetime", "modules.tickets.datetime",
                       "modules.dashboard.datetime", "modules.automation.datetime"):
            clock = self._patch(patch(target, wraps=datetime))
            clock.now.return_value = self.now
        self.fields = {
            "service_type": "护理员上门", "appointment_time": "明天下午两点", "address": "模拟地址",
            "customer_name": "虚构客户", "phone": "演示电话001", "urgency": "普通",
        }

    def _patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def parsed(self, confidence=0.9):
        return {field: {"value": value, "confidence": confidence} for field, value in self.fields.items()}

    def add_ticket(self, ticket_id, status="待确认", days_ago=0, confidence=None, **changes):
        ticket = {
            "id": ticket_id, "customer_input": "虚构需求", **self.fields, "status": status,
            "created_time": (self.now - timedelta(days=days_ago)).isoformat(),
            "review_required": status == "人工审核",
            "confidence_score": json.dumps(self.parsed(confidence)) if confidence is not None else "{}",
            **changes,
        }
        db.save_ticket(ticket)
        return ticket

    def open_app(self):
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
        self.assertFalse(app.exception)
        return app

    def click(self, app, label):
        next(button for button in app.button if button.label == label).click().run(timeout=30)
        self.assertFalse(app.exception)

    def test_empty_database_returns_zero_rates_and_complete_thirty_day_axis(self):
        metrics = analytics.get_operational_metrics()
        for field in ("total_tickets", "today_new_tickets", "completed_count", "abnormal_count",
                      "review_required_count", "ai_auto_passed_count", "demo_ticket_count"):
            self.assertEqual(metrics[field], 0)
        self.assertEqual(metrics["completion_rate"], 0)
        self.assertEqual(metrics["ai_auto_processing_rate"], 0)
        self.assertIsNone(metrics["avg_response_time"])
        self.assertEqual(len(metrics["daily_trend"]), 30)
        self.assertEqual(metrics["daily_trend"][0], {"date": "2026-09-13", "new_tickets": 0})
        self.assertEqual(metrics["daily_trend"][-1], {"date": "2026-10-12", "new_tickets": 0})

    def test_all_requested_statistics_use_current_status_and_original_parse(self):
        self.add_ticket("a", "已完成", confidence=0.9, response_time=0.2)
        self.add_ticket("b", "人工审核", confidence=0.55, response_time=0.6, service_type="居家保洁")
        self.add_ticket("c", "已完成", days_ago=1, confidence=0.55, service_type="康复服务")
        self.add_ticket("d", "异常", days_ago=29, confidence=0.9, response_time=1.2, service_type="维修服务")
        self.add_ticket("e", days_ago=30, service_type="维修服务")
        before = db.get_all_tickets()
        metrics = analytics.get_operational_metrics()
        self.assertEqual(metrics["total_tickets"], 5)
        self.assertEqual(metrics["today_new_tickets"], 2)
        self.assertEqual(metrics["completed_count"], 2)
        self.assertEqual(metrics["abnormal_count"], 1)
        self.assertEqual(metrics["review_required_count"], 1)
        self.assertEqual(metrics["ai_auto_passed_count"], 2)
        self.assertAlmostEqual(metrics["avg_response_time"], 2 / 3)
        self.assertAlmostEqual(metrics["completion_rate"], 0.4)
        self.assertAlmostEqual(metrics["ai_auto_processing_rate"], 0.4)
        self.assertEqual(sum(item["new_tickets"] for item in metrics["daily_trend"]), 4)
        self.assertEqual(metrics["service_type_counts"], {"维修服务": 2, "居家保洁": 1, "护理员上门": 1, "康复服务": 1})
        self.assertEqual(db.get_all_tickets(), before)
        self.sdk.assert_not_called()

    def test_human_review_does_not_turn_low_original_confidence_into_auto_pass(self):
        ticket = create_ticket("虚构需求", self.parsed(0.55))
        self.assertEqual(analytics.get_operational_metrics()["review_required_count"], 1)
        reviewed = review_ticket(ticket["id"], self.fields)
        self.assertEqual(reviewed["status"], "待确认")
        self.assertEqual(analytics.get_operational_metrics()["review_required_count"], 0)
        self.assertEqual(analytics.get_operational_metrics()["ai_auto_passed_count"], 0)
        db.update_ticket_status(ticket["id"], "已完成")
        self.assertEqual(analytics.get_operational_metrics()["completed_count"], 1)
        self.assertEqual(analytics.get_operational_metrics()["ai_auto_processing_rate"], 0)

    def test_missing_malformed_or_low_confidence_snapshots_are_not_auto_passed(self):
        cases = ("{}", "invalid-json", "[]", json.dumps(self.parsed(0.55)))
        for index, original in enumerate(cases):
            self.add_ticket(str(index), confidence_score=original)
        self.add_ticket("boundary", confidence=0.7)
        metrics = analytics.get_operational_metrics()
        self.assertEqual(metrics["ai_auto_passed_count"], 1)
        self.assertAlmostEqual(metrics["ai_auto_processing_rate"], 0.2)

    def test_today_and_trend_boundaries_use_beijing_time_and_include_zero_days(self):
        for index, timestamp in enumerate((
            "2026-10-11T15:59:59+00:00", "2026-10-11T16:00:00+00:00",
            "2026-10-12T15:59:59+00:00", "2026-10-12T16:00:00+00:00",
            "2026-10-12T00:00:00", "2026-09-12T23:59:59+08:00",
        )):
            self.add_ticket(str(index), created_time=timestamp)
        metrics = analytics.get_operational_metrics()
        self.assertEqual(metrics["today_new_tickets"], 3)
        self.assertEqual(sum(day["new_tickets"] for day in metrics["daily_trend"]), 4)
        self.assertEqual(metrics["daily_trend"][0]["new_tickets"], 0)
        self.assertEqual(metrics["daily_trend"][-1]["new_tickets"], 3)

    def test_unknown_durations_are_not_averaged_as_zero_and_snapshot_is_read_once(self):
        self.add_ticket("unknown")
        self.assertIsNone(analytics.get_operational_metrics()["avg_response_time"])
        self.add_ticket("measured", response_time=0)
        with patch.object(analytics, "get_all_tickets", wraps=db.get_all_tickets) as reader:
            metrics = analytics.get_operational_metrics()
        reader.assert_called_once_with()
        self.assertEqual(metrics["avg_response_time"], 0)

    def test_invalid_timestamps_and_database_errors_are_reported(self):
        self.add_ticket("bad", created_time="invalid-date")
        with self.assertRaises(ValueError):
            analytics.get_operational_metrics()
        with patch.object(analytics, "get_all_tickets", side_effect=sqlite3.OperationalError("offline-db-error")):
            with self.assertRaises(sqlite3.OperationalError):
                analytics.get_operational_metrics()

    def test_seed_covers_every_service_status_and_all_thirty_days_without_api_metrics(self):
        self.assertEqual(seed.seed_demo_data(), 120)
        tickets = db.get_all_tickets()
        self.assertEqual(len(tickets), 120)
        self.assertEqual(len({item["id"] for item in tickets}), 120)
        self.assertEqual({item["service_type"] for item in tickets}, set(seed.SERVICE_TYPES))
        self.assertEqual({item["status"] for item in tickets}, set(seed.STATUSES))
        self.assertEqual({datetime.fromisoformat(item["created_time"]).date() for item in tickets},
                         {self.now.date() - timedelta(days=offset) for offset in range(30)})
        for ticket in tickets:
            self.assertTrue(ticket["id"].startswith("DEMO-"))
            self.assertTrue(ticket["customer_input"].startswith("[模拟业务数据]"))
            self.assertEqual(ticket["review_required"], ticket["status"] == "人工审核")
            self.assertEqual(ticket["token_used"], 0)
            self.assertIsNone(ticket["response_time"])
        metrics = analytics.get_operational_metrics()
        self.assertEqual(metrics["completed_count"], 24)
        self.assertEqual(metrics["abnormal_count"], 24)
        self.assertEqual(metrics["review_required_count"], 24)
        self.assertEqual(metrics["demo_ticket_count"], 120)
        self.assertGreater(metrics["ai_auto_passed_count"], 0)
        self.assertTrue(all(day["new_tickets"] > 0 for day in metrics["daily_trend"]))
        self.assertFalse(ai_logging.LOG_PATH.exists())
        self.sdk.assert_not_called()

    def test_seed_repeat_and_nonempty_database_never_append_or_overwrite(self):
        original = self.add_ticket("existing")
        before = db.get_all_tickets()
        self.assertEqual(seed.seed_demo_data(), 0)
        self.assertEqual(db.get_all_tickets(), before)
        self.assertEqual(db.get_ticket(original["id"])["customer_input"], "虚构需求")

    def test_two_simultaneous_seed_requests_load_only_one_batch(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: seed.seed_demo_data(), range(2)))
        self.assertEqual(sorted(results), [0, 120])
        before = db.get_all_tickets()
        self.assertEqual(seed.seed_demo_data(), 0)
        self.assertEqual(db.get_all_tickets(), before)

    def test_seed_rolls_back_entire_batch_on_insert_failure(self):
        first, second = seed.generate_demo_tickets()[:2]
        second["id"] = first["id"]
        with patch.object(seed, "generate_demo_tickets", return_value=[first, second]):
            with self.assertRaises(sqlite3.IntegrityError):
                seed.seed_demo_data()
        self.assertEqual(db.get_all_tickets(), [])

    def test_seed_rejects_counts_below_requirement_or_wrong_types(self):
        for count in (99, 0, -1, True, 100.0, "100"):
            with self.subTest(count=count), self.assertRaises(ValueError):
                seed.generate_demo_tickets(count)
        self.assertFalse(db.DB_PATH.exists())

    def test_empty_page_loads_data_once_and_renders_all_three_charts(self):
        app = self.open_app()
        self.assertTrue(any(item.value == "当前暂无业务数据，是否加载演示数据" for item in app.info))
        self.click(app, "加载演示数据")
        self.assertEqual(len(db.get_all_tickets()), 120)
        self.assertFalse(any(button.label == "加载演示数据" for button in app.button))
        metrics = {item.label: item.value for item in app.metric}
        self.assertEqual(metrics["工单总数"], "120")
        self.assertEqual(metrics["完成率"], "20%")
        self.assertEqual(metrics["人工审核数量"], "24")
        self.assertEqual(metrics["AI解析成功率"], "暂无真实调用")
        self.assertEqual(metrics["平均响应时间"], "暂无测量")
        charts = app.get("vega_lite_chart")
        self.assertEqual(len(charts), 3)
        pie = json.loads(charts[0].proto.spec)
        self.assertEqual(pie["mark"]["type"], "arc")
        self.assertEqual(pie["encoding"]["theta"]["field"], "工单数量")
        app.run(timeout=30)
        self.assertFalse(app.exception)
        self.assertEqual(len(db.get_all_tickets()), 120)
        self.sdk.assert_not_called()

    def test_failed_seed_from_page_keeps_empty_database_and_no_success_message(self):
        app = self.open_app()
        with patch.object(seed, "seed_demo_data", side_effect=sqlite3.OperationalError("offline-load-error")):
            self.click(app, "加载演示数据")
        self.assertTrue(any("演示数据加载失败" in item.value for item in app.error))
        self.assertFalse(any("已加载" in item.value for item in app.success))
        self.assertEqual(db.get_all_tickets(), [])
        self.sdk.assert_not_called()


if __name__ == "__main__":
    unittest.main()
