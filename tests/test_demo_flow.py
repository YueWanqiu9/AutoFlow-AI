"""完整演示验收：真实业务接口、独立数据库、可重复运行与页面回看。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import config
from database import db, seed_demo_flow as demo
from modules import ai_client, ai_logging, ai_parser, staff, tickets
from modules.dashboard import get_dashboard_stats


class DemoFlowTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self._patch(patch.object(db, "DB_PATH", self.directory / "business.db"))
        self._patch(patch.object(demo, "DEMO_DB_PATH", self.directory / "demo" / "flow.db"))
        self._patch(patch.object(ai_logging, "LOG_PATH", self.directory / "business_log.json"))
        self._patch(patch.object(config, "DEMO_MODE", True))
        self._patch(patch.object(ai_parser, "load_dotenv"))
        self._patch(patch.dict(os.environ, {"OPENAI_API_KEY": ""}))
        self.parser_sdk = self._patch(patch.object(ai_parser, "OpenAI"))
        self.client_sdk = self._patch(patch.object(ai_client, "OpenAI"))

    def _patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def query(self, sql):
        with closing(sqlite3.connect(demo.DEMO_DB_PATH)) as connection:
            return connection.execute(sql).fetchall()

    def open_app(self):
        app = AppTest.from_file(str(demo.ROOT / "app.py")).run(timeout=30)
        self.assertFalse(app.exception)
        return app

    def click_demo(self, app):
        next(button for button in app.button if button.label == "体验完整业务流程").click().run(timeout=30)
        self.assertFalse(app.exception)

    def test_complete_case_uses_one_ticket_and_finishes_service(self):
        result = demo.run_demo_flow()
        self.assertEqual(result["customer_request"], demo.DEMO_REQUEST)
        self.assertEqual(result["dispatch"]["status"], "completed")
        self.assertEqual(result["staff"]["status"], "available")
        self.assertEqual(result["ticket"]["priority_final"], "高")
        self.assertEqual(result["ticket"]["advice_decision"], "accepted")
        self.assertEqual(result["dispatch"]["ticket_id"], result["ticket"]["id"])
        self.assertEqual(self.query("SELECT COUNT(*) FROM tickets"), [(1,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM dispatches"), [(1,)])
        self.assertEqual(result["ticket"]["status"], "待确认")  # 工单人工状态独立保留
        self.assertFalse(db.DB_PATH.exists())
        self.assertFalse(ai_logging.LOG_PATH.exists())

    def test_all_persisted_demo_records_have_explicit_marker(self):
        result = demo.run_demo_flow()
        self.assertEqual(result["is_demo"], 1)
        for table in demo.DEMO_TABLES:
            with self.subTest(table=table):
                self.assertGreater(self.query(f"SELECT COUNT(*) FROM {table}")[0][0], 0)
                self.assertEqual(self.query(f"SELECT COUNT(*) FROM {table} WHERE is_demo IS NULL OR is_demo <> 1"), [(0,)])
        self.assertTrue(all(step["is_demo"] == 1 for step in result["steps"]))

    def test_original_business_rows_schema_logs_and_metrics_unchanged(self):
        record = tickets.create_ticket("隔离测试用业务工单", {"service_type": "居家保洁"})
        staff.create_staff("隔离测试用人员", skills=["居家保洁"], service_area="测试区域")
        ai_logging.LOG_PATH.write_text("[]", encoding="utf-8")
        before_metrics = get_dashboard_stats()
        before_bytes = db.DB_PATH.read_bytes()
        before_log = ai_logging.LOG_PATH.read_bytes()
        demo.run_demo_flow()
        self.assertEqual(db.DB_PATH.read_bytes(), before_bytes)
        self.assertEqual(ai_logging.LOG_PATH.read_bytes(), before_log)
        self.assertEqual(get_dashboard_stats(), before_metrics)
        self.assertEqual(db.get_all_tickets(), [record])

    def test_step_sequence_and_saved_state_snapshots(self):
        steps = demo.run_demo_flow()["steps"]
        self.assertEqual([step["number"] for step in steps], list(range(1, 11)))
        self.assertEqual([step["title"] for step in steps], list(demo.STEP_TITLES))
        self.assertEqual(steps[1]["details"]["phone"], "")
        self.assertEqual(steps[1]["details"]["status"], "人工审核")
        self.assertEqual(set(steps[2]["details"]["missing"]), {"phone", "customer_name"})
        self.assertEqual(steps[3]["details"]["ticket"]["phone"], "演示电话001")
        self.assertEqual(steps[7]["details"]["dispatch"]["status"], "confirmed")
        self.assertEqual(steps[7]["details"]["staff"]["status"], "busy")
        self.assertEqual(steps[8]["details"]["status"], "in_service")
        self.assertIsNone(steps[8]["details"]["completed_at"])
        self.assertEqual(steps[9]["details"]["dispatch"]["status"], "completed")

    def test_two_rounds_supplement_original_ticket(self):
        result = demo.run_demo_flow()
        messages = result["steps"][3]["details"]["conversation"]["messages"]
        self.assertEqual([item["content"] for item in messages if item["role"] == "user"],
                         ["姓名：演示客户", "电话：演示电话001"])
        self.assertEqual({item["ticket_id"] for item in messages}, {result["ticket"]["id"]})
        self.assertEqual(self.query("SELECT field_name, status FROM ticket_missing_fields ORDER BY field_name"),
                         [("customer_name", "filled"), ("phone", "filled")])

    def test_decisions_and_service_transitions_have_ordered_events(self):
        result = demo.run_demo_flow()
        types = [event["event_type"] for event in result["events"]]
        expected = ("ticket_created", "conversation_started", "field_filled", "conversation_completed",
                    "ticket_reviewed", "priority_confirmed", "advice_generated", "advice_reviewed",
                    "dispatch_created", "staff_assigned", "dispatch_accepted", "service_started", "service_completed")
        positions = [types.index(name) for name in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual({event["ticket_id"] for event in result["events"]}, {result["ticket"]["id"]})
        for name in ("priority_confirmed", "advice_reviewed", "dispatch_created"):
            event = next(item for item in result["events"] if item["event_type"] == name)
            self.assertEqual(event["actor_type"], "operator")
            self.assertIn("演示", event["reason"])

    def test_repeat_run_reuses_ids_events_and_database_bytes(self):
        first = demo.run_demo_flow()
        data = demo.DEMO_DB_PATH.read_bytes()
        with patch.object(demo.subprocess, "run") as worker:
            self.assertEqual(demo.run_demo_flow(), first)
            self.assertEqual(demo.load_demo_flow(), first)
            worker.assert_not_called()
        self.assertEqual(demo.DEMO_DB_PATH.read_bytes(), data)

    def test_concurrent_clicks_produce_one_case(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: demo.run_demo_flow(), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.query("SELECT COUNT(*) FROM tickets"), [(1,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM demo_flow_snapshot"), [(1,)])

    def test_complete_demo_stays_offline_when_workbench_real_mode_enabled(self):
        config.DEMO_MODE = False
        os.environ["OPENAI_API_KEY"] = "offline-fixture-key"
        original_db, original_log = db.DB_PATH, ai_logging.LOG_PATH
        result = demo.run_demo_flow()
        self.assertEqual(result["ticket"]["priority_source"], "demo")
        self.assertEqual(result["ticket"]["advice_source"], "demo")
        self.assertEqual(result["ticket"]["token_used"], 0)
        self.assertIsNone(result["ticket"]["response_time"])
        self.assertEqual(result["steps"][6]["details"]["explanation"]["source"], "rule")
        self.assertFalse(config.DEMO_MODE)
        self.assertEqual((db.DB_PATH, ai_logging.LOG_PATH), (original_db, original_log))
        self.assertFalse(ai_logging.LOG_PATH.exists())
        self.parser_sdk.assert_not_called()
        self.client_sdk.assert_not_called()

    def test_rejects_business_database_as_demo_target(self):
        db.init_db()
        data = db.DB_PATH.read_bytes()
        with patch.object(demo, "DEMO_DB_PATH", db.DB_PATH), self.assertRaises(demo.DemoFlowError):
            demo.run_demo_flow()
        self.assertEqual(db.DB_PATH.read_bytes(), data)

    def test_worker_failure_keeps_no_partial_case_and_can_retry(self):
        with patch.object(demo.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaises(demo.DemoFlowError):
                demo.run_demo_flow()
        self.assertFalse(demo.DEMO_DB_PATH.exists())
        self.assertEqual(list(demo.DEMO_DB_PATH.parent.glob("build-*")), [])
        self.assertEqual(demo.run_demo_flow()["dispatch"]["status"], "completed")

    def test_timeout_is_reported_without_partial_result(self):
        with patch.object(demo.subprocess, "run", side_effect=subprocess.TimeoutExpired("demo", 60)):
            with self.assertRaisesRegex(demo.DemoFlowError, "工作台数据未受影响"):
                demo.run_demo_flow()
        self.assertFalse(demo.DEMO_DB_PATH.exists())

    def test_missing_snapshot_is_read_only_and_corruption_is_not_overwritten(self):
        self.assertIsNone(demo.load_demo_flow())
        self.assertFalse(demo.DEMO_DB_PATH.parent.exists())
        demo.DEMO_DB_PATH.parent.mkdir()
        demo.DEMO_DB_PATH.write_bytes(b"corrupt-demo-fixture")
        with self.assertRaises(demo.DemoFlowError):
            demo.run_demo_flow()
        self.assertEqual(demo.DEMO_DB_PATH.read_bytes(), b"corrupt-demo-fixture")

    def test_homepage_has_five_parts_and_does_not_start_demo_on_load(self):
        app = self.open_app()
        self.assertEqual([tab.label for tab in app.tabs], ["演示首页", "运营工作台"])
        home = app.tabs[0]
        headings = [item.value for item in home.subheader]
        self.assertEqual(headings, ["一键 Demo · 五分钟理解业务闭环", "核心模块", "业务流程", "运营 Dashboard", "技术架构"])
        self.assertFalse(demo.DEMO_DB_PATH.exists())
        self.assertEqual(db.get_all_tickets(), [])
        self.parser_sdk.assert_not_called()

    def test_ui_click_navigation_and_rerun_leave_workbench_untouched(self):
        app = self.open_app()
        self.click_demo(app)
        self.assertEqual(db.get_all_tickets(), [])
        metrics = {item.label: item.value for item in app.metric}
        self.assertEqual(metrics["演示完成服务"], "1")
        self.assertEqual(metrics["工单总数"], "0")
        data = demo.DEMO_DB_PATH.read_bytes()
        with patch.object(demo.subprocess, "run") as worker:
            for number in range(2, 11):
                app.select_slider(key="full_demo_step").set_value(number).run(timeout=30)
                self.assertFalse(app.exception)
                self.assertTrue(any(f"{number}. {demo.STEP_TITLES[number - 1]}" in item.value for item in app.markdown))
            app.run(timeout=30)
            self.click_demo(app)
            worker.assert_not_called()
        self.assertEqual(demo.DEMO_DB_PATH.read_bytes(), data)
        self.assertEqual(db.get_all_tickets(), [])
        self.assertFalse(ai_logging.LOG_PATH.exists())

    def test_page_failure_is_visible_and_does_not_auto_retry_on_refresh(self):
        app = self.open_app()
        with patch("modules.demo_view.run_demo_flow", side_effect=demo.DemoFlowError("演示执行失败，请重试。")) as builder:
            self.click_demo(app)
            self.assertTrue(any("演示执行失败" in item.value for item in app.error))
            app.run(timeout=30)
            builder.assert_called_once()
        self.assertFalse(demo.DEMO_DB_PATH.exists())


if __name__ == "__main__":
    unittest.main()
