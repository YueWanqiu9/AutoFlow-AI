"""日报统计使用临时数据库和固定日期，避免真实数据与系统时钟影响。"""

import sqlite3
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from database import db
from modules import automation


class DailyReportTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self._patch(patch.object(db, "DB_PATH", Path(directory.name) / "autoflow.db"))
        clock = self._patch(patch.object(automation, "datetime", wraps=datetime))
        clock.now.return_value = datetime(2026, 10, 11, 12, tzinfo=automation.REPORT_TIMEZONE)

    def _patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def add_ticket(self, ticket_id, status, created_time="2026-10-11T10:00:00+08:00"):
        ticket = {
            "id": ticket_id,
            "customer_input": "虚构测试需求",
            "status": status,
            "created_time": created_time,
        }
        db.save_ticket(ticket)

    def test_empty_database_returns_zero_counts(self):
        report = automation.generate_daily_report()
        self.assertEqual(report["report_date"], "2026-10-11")
        for scope in ("today", "overall"):
            self.assertEqual(report[scope]["total_tickets"], 0)
            self.assertTrue(all(value == 0 for value in report[scope].values()))

    def test_status_counts_and_pending_summary_use_current_status(self):
        for ticket_id, status in enumerate(("已完成", "待补充信息", "待确认", "异常", "待处理", "已取消")):
            self.add_ticket(str(ticket_id), status)
        before = db.get_all_tickets()
        report = automation.generate_daily_report()
        expected = {
            "total_tickets": 6,
            "completed": 1,
            "pending_information": 1,
            "pending_confirmation": 1,
            "pending_review": 0,
            "abnormal": 1,
            "pending": 3,
        }
        self.assertEqual(report["today"], expected)
        self.assertEqual(report["overall"], expected)
        self.assertEqual(db.get_all_tickets(), before)

    def test_daily_statistics_exclude_previous_and_future_days(self):
        self.add_ticket("old", "已完成", "2026-10-10T23:59:59+08:00")
        self.add_ticket("today", "待确认")
        self.add_ticket("future", "异常", "2026-10-12T00:00:00+08:00")
        report = automation.generate_daily_report()
        self.assertEqual(report["today"]["total_tickets"], 1)
        self.assertEqual(report["today"]["pending_confirmation"], 1)
        self.assertEqual(report["today"]["completed"], 0)
        self.assertEqual(report["today"]["abnormal"], 0)
        self.assertEqual(report["overall"]["total_tickets"], 3)
        self.assertEqual(report["overall"]["completed"], 1)
        self.assertEqual(report["overall"]["abnormal"], 1)

    def test_day_boundaries_are_based_on_beijing_time(self):
        times = (
            "2026-10-10T15:59:59+00:00",  # 北京昨天最后一秒
            "2026-10-10T16:00:00+00:00",  # 北京今天第一秒
            "2026-10-11T15:59:59+00:00",  # 北京今天最后一秒
            "2026-10-11T16:00:00+00:00",  # 北京明天第一秒
        )
        for index, created_time in enumerate(times):
            self.add_ticket(str(index), "待确认", created_time)
        report = automation.generate_daily_report()
        self.assertEqual(report["today"]["total_tickets"], 2)
        self.assertEqual(report["overall"]["total_tickets"], 4)

    def test_legacy_naive_timestamps_are_treated_as_beijing_time(self):
        self.add_ticket("legacy", "已完成", "2026-10-11T00:00:00")
        self.assertEqual(automation.generate_daily_report()["today"]["completed"], 1)

    def test_status_updates_are_reflected_without_changing_creation_date(self):
        self.add_ticket("today", "待确认")
        self.add_ticket("old", "待确认", "2026-10-10T10:00:00+08:00")
        self.assertTrue(db.update_ticket_status("today", "已完成"))
        self.assertTrue(db.update_ticket_status("old", "已完成"))
        report = automation.generate_daily_report()
        self.assertEqual(report["today"]["completed"], 1)
        self.assertEqual(report["today"]["pending"], 0)
        self.assertEqual(report["overall"]["completed"], 2)

    def test_dispatch_and_in_service_tickets_remain_in_pending_count(self):
        self.add_ticket("dispatch", "待派单")
        self.add_ticket("in-service", "服务中")
        report = automation.generate_daily_report()
        for scope in ("today", "overall"):
            self.assertEqual(report[scope]["total_tickets"], 2)
            self.assertEqual(report[scope]["pending"], 2)
            self.assertEqual(report[scope]["completed"], 0)

    def test_manual_review_tickets_are_counted_in_pending_and_review(self):
        self.add_ticket("review", "人工审核")
        self.add_ticket("confirmed", "待确认")
        report = automation.generate_daily_report()
        for scope in ("today", "overall"):
            self.assertEqual(report[scope]["total_tickets"], 2)
            self.assertEqual(report[scope]["pending"], 2)
            self.assertEqual(report[scope]["pending_review"], 1)
            self.assertEqual(report[scope]["completed"], 0)

    def test_invalid_timestamp_is_not_silently_ignored(self):
        self.add_ticket("bad-date", "待确认", "not-a-date")
        with self.assertRaises(ValueError):
            automation.generate_daily_report()

    def test_database_read_errors_propagate(self):
        with patch.object(automation, "get_all_tickets", side_effect=sqlite3.OperationalError("test")):
            with self.assertRaises(sqlite3.OperationalError):
                automation.generate_daily_report()


if __name__ == "__main__":
    unittest.main()
