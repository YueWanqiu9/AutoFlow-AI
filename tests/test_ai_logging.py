"""AI 日志的原子写入、读取与统计，不使用实际客户日志。"""

import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from modules import ai_logging


class AILoggingTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "logs" / "ai_calls.json"
        patcher = patch.object(ai_logging, "LOG_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def record(self, **changes):
        return {
            "timestamp": "2026-10-12T10:00:00+08:00", "input": "虚构需求", "output": {},
            "status": "success", "error": "", "api_called": True,
            "token_used": 100, "response_time": 0.2, **changes,
        }

    def test_missing_log_returns_empty_stats(self):
        self.assertEqual(ai_logging.read_ai_calls(), [])
        self.assertEqual(ai_logging.get_ai_call_stats(), {
            "ai_call_count": 0, "avg_response_time": None,
            "ai_success_count": 0, "ai_success_rate": None,
        })

    def test_append_preserves_unicode_and_prior_records(self):
        first = self.record(input="演示客户的虚构需求")
        second = self.record(status="api_error", error="请求失败", response_time=0.6)
        ai_logging.append_ai_call(first)
        ai_logging.append_ai_call(second)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), [first, second])
        self.assertEqual(ai_logging.get_ai_call_stats(), {
            "ai_call_count": 2, "avg_response_time": 0.4,
            "ai_success_count": 1, "ai_success_rate": 0.5,
        })

    def test_configuration_failure_is_not_counted_as_api_attempt(self):
        ai_logging.append_ai_call(self.record(status="configuration_error", api_called=False, response_time=None))
        self.assertEqual(ai_logging.get_ai_call_stats(), {
            "ai_call_count": 0, "avg_response_time": None,
            "ai_success_count": 0, "ai_success_rate": None,
        })

    def test_no_measurement_does_not_become_zero_second_average(self):
        ai_logging.append_ai_call(self.record(response_time=None))
        self.assertEqual(ai_logging.get_ai_call_stats(), {
            "ai_call_count": 1, "avg_response_time": None,
            "ai_success_count": 1, "ai_success_rate": 1.0,
        })

    def test_invalid_measurements_are_excluded_from_average(self):
        self.path.parent.mkdir()
        self.path.write_text(json.dumps([
            self.record(response_time=value) for value in (None, True, -1, "0.2", float("nan"))
        ]), encoding="utf-8")
        self.assertEqual(ai_logging.get_ai_call_stats(), {
            "ai_call_count": 5, "avg_response_time": None,
            "ai_success_count": 5, "ai_success_rate": 1.0,
        })

    def test_success_rate_includes_failed_calls_and_excludes_demo_and_configuration(self):
        for status in ("success", "api_error", "timeout", "json_error"):
            ai_logging.append_ai_call(self.record(status=status))
        for status in ("demo", "configuration_error"):
            for api_called in (False, True):
                ai_logging.append_ai_call(self.record(status=status, api_called=api_called))
        stats = ai_logging.get_ai_call_stats()
        self.assertEqual(stats["ai_call_count"], 4)
        self.assertEqual(stats["ai_success_count"], 1)
        self.assertEqual(stats["ai_success_rate"], 0.25)
        self.assertAlmostEqual(stats["avg_response_time"], 0.2)

    def test_historical_logs_without_api_flag_exclude_demo_and_configuration(self):
        self.path.parent.mkdir()
        self.path.write_text(json.dumps([
            {"status": "success", "response_time": 0.2},
            {"status": "api_error", "response_time": 0.6},
            {"status": "legacy_unknown", "response_time": 0.4},
            {"status": "demo", "response_time": 900},
            {"status": "configuration_error", "response_time": 900},
        ]), encoding="utf-8")
        stats = ai_logging.get_ai_call_stats()
        self.assertEqual(stats["ai_call_count"], 3)
        self.assertEqual(stats["ai_success_count"], 1)
        self.assertAlmostEqual(stats["ai_success_rate"], 1 / 3)
        self.assertAlmostEqual(stats["avg_response_time"], 0.4)

    def test_demo_only_log_has_no_actual_success_rate(self):
        ai_logging.append_ai_call(self.record(status="demo", api_called=False, response_time=None))
        self.assertEqual(ai_logging.get_ai_call_stats(), {
            "ai_call_count": 0, "avg_response_time": None,
            "ai_success_count": 0, "ai_success_rate": None,
        })

    def test_corrupt_existing_log_is_not_overwritten(self):
        self.path.parent.mkdir()
        self.path.write_text("corrupt-log", encoding="utf-8")
        with self.assertRaises(ValueError):
            ai_logging.append_ai_call(self.record())
        self.assertEqual(self.path.read_text(encoding="utf-8"), "corrupt-log")

    def test_non_serializable_entry_preserves_old_log_and_cleans_temporary_file(self):
        ai_logging.append_ai_call(self.record())
        before = self.path.read_text(encoding="utf-8")
        with self.assertRaises(ValueError):
            ai_logging.append_ai_call(self.record(response_time=float("nan")))
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_multiple_threads_do_not_lose_records(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda number: ai_logging.append_ai_call(self.record(input=f"虚构需求{number}")), range(12)))
        self.assertEqual(len(ai_logging.read_ai_calls()), 12)
        self.assertEqual(ai_logging.get_ai_call_stats()["ai_call_count"], 12)


if __name__ == "__main__":
    unittest.main()
