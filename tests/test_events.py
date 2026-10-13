"""工单事件、事务回滚、迁移和页面展示；只使用临时数据库。"""

import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from database import db, seed
from modules import ai_logging, ai_parser, events
from modules.tickets import change_ticket_status, create_ticket, review_ticket


class TicketEventTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self._patch(patch.object(db, "DB_PATH", self.directory / "autoflow.db"))
        self._patch(patch.object(ai_logging, "LOG_PATH", self.directory / "ai_calls.json"))
        self._patch(patch.object(ai_parser, "get_ai_status", return_value={
            "demo_mode": True, "api_key_configured": False,
            "configuration_error": "", "model": "demo-parser",
        }))
        self.sdk = self._patch(patch.object(ai_parser, "OpenAI"))
        self.fields = {
            "service_type": "居家保洁", "appointment_time": "周六上午",
            "address": "示例区演示小区", "customer_name": "演示客户",
            "phone": "演示电话001", "urgency": "普通",
        }

    def _patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def create_pending_review(self):
        parsed = {
            field: {"value": value, "confidence": 0.9}
            for field, value in self.fields.items()
        }
        parsed["phone"] = {"value": "", "confidence": 0.0}
        return create_ticket("需要补充电话的虚构需求", parsed)

    def reject_event_writes(self):
        db.init_db()
        with db._connection() as connection:
            connection.execute(
                """CREATE TRIGGER reject_events BEFORE INSERT ON ticket_events
                   BEGIN SELECT RAISE(ABORT, 'test event failure'); END"""
            )

    def open_app(self):
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
        self.assertFalse(app.exception)
        return app

    def test_creation_records_final_status_and_business_fields(self):
        ticket = create_ticket("虚构保洁需求", self.fields)
        history = events.get_ticket_events(ticket["id"])
        self.assertEqual(len(history), 2)
        self.assertEqual(history[1]["event_type"], "priority_generated")
        event = history[0]
        self.assertEqual(event["event_type"], "ticket_created")
        self.assertEqual(event["actor_type"], "system")
        self.assertEqual(event["before_data"], {})
        self.assertEqual(event["after_data"], {**self.fields, "status": "待确认", "review_required": False, "advice_decision": "unreviewed"})
        self.assertTrue(event["operation_id"])
        self.assertEqual(datetime.fromisoformat(event["created_at"]).utcoffset(), timedelta(hours=8))
        self.assertEqual(db.get_ticket(ticket["id"]), ticket)

    def test_status_change_records_only_changed_values(self):
        ticket = create_ticket("虚构需求", self.fields)
        self.assertTrue(change_ticket_status(ticket["id"], "服务中"))
        event = events.get_ticket_events(ticket["id"])[-1]
        self.assertEqual(event["event_type"], "status_changed")
        self.assertEqual(event["actor_type"], "operator")
        self.assertEqual(event["before_data"], {"status": "待确认"})
        self.assertEqual(event["after_data"], {"status": "服务中"})
        self.assertEqual(db.get_ticket(ticket["id"])["status"], "服务中")

    def test_direct_database_status_updates_are_also_recorded(self):
        ticket = create_ticket("虚构需求", self.fields)
        self.assertTrue(db.update_ticket_status(ticket["id"], "人工审核"))
        event = events.get_ticket_events(ticket["id"])[-1]
        self.assertEqual(event["before_data"], {"status": "待确认", "review_required": False})
        self.assertEqual(event["after_data"], {"status": "人工审核", "review_required": True})

    def test_review_records_field_status_and_flag_changes_without_changing_ai_snapshot(self):
        ticket = self.create_pending_review()
        reviewed = review_ticket(ticket["id"], self.fields)
        self.assertEqual(reviewed["status"], "待确认")
        event = events.get_ticket_events(ticket["id"])[-1]
        self.assertEqual(event["event_type"], "ticket_reviewed")
        self.assertEqual(event["actor_type"], "operator")
        self.assertEqual(event["before_data"], {"phone": "", "status": "人工审核", "review_required": True})
        self.assertEqual(event["after_data"], {"phone": "演示电话001", "status": "待确认", "review_required": False})
        self.assertEqual(db.get_ticket(ticket["id"])["confidence_score"], ticket["confidence_score"])
        self.assertIsNone(review_ticket(ticket["id"], self.fields))
        self.assertEqual(len(events.get_ticket_events(ticket["id"])), 3)

    def test_review_still_records_pending_information_when_fields_remain_missing(self):
        ticket = self.create_pending_review()
        review_ticket(ticket["id"], {**self.fields, "phone": ""})
        event = events.get_ticket_events(ticket["id"])[-1]
        self.assertEqual(event["after_data"], {"status": "待补充信息", "review_required": False})

    def test_query_filters_tickets_and_uses_id_to_break_equal_time_ties(self):
        fixed_time = datetime(2026, 10, 13, 12, tzinfo=timezone(timedelta(hours=8)))
        with patch.object(events, "datetime", wraps=datetime) as clock:
            clock.now.return_value = fixed_time
            first = create_ticket("第一条虚构需求", self.fields)
            second = create_ticket("第二条虚构需求", self.fields)
            change_ticket_status(first["id"], "服务中")
            change_ticket_status(first["id"], "已完成")
        history = events.get_ticket_events(first["id"])
        self.assertEqual([item["after_data"]["status"] for item in history if "status" in item["after_data"]], ["待确认", "服务中", "已完成"])
        self.assertEqual([item["id"] for item in history], sorted(item["id"] for item in history))
        self.assertEqual(len({item["operation_id"] for item in history}), 4)
        self.assertEqual(len(events.get_ticket_events(second["id"])), 2)
        self.assertEqual(events.get_ticket_events("unknown"), [])
        self.assertEqual(events.get_ticket_events("' OR 1=1 --"), [])

    def test_no_change_or_rejected_operations_do_not_add_events(self):
        ticket = create_ticket("虚构需求", self.fields)
        original = events.get_ticket_events(ticket["id"])
        self.assertTrue(change_ticket_status(ticket["id"], " 待确认 "))
        self.assertFalse(change_ticket_status("unknown", "已完成"))
        self.assertIsNone(review_ticket(ticket["id"], self.fields))
        with self.assertRaises(ValueError):
            change_ticket_status(ticket["id"], " ")
        self.assertEqual(events.get_ticket_events(ticket["id"]), original)
        pending = self.create_pending_review()
        with self.assertRaises(ValueError):
            change_ticket_status(pending["id"], "已完成")
        self.assertEqual(len(events.get_ticket_events(pending["id"])), 2)

    def test_creation_is_rolled_back_when_event_insert_fails(self):
        self.reject_event_writes()
        with self.assertRaises(sqlite3.IntegrityError):
            create_ticket("虚构需求", self.fields)
        self.assertEqual(db.get_all_tickets(), [])
        with db._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ticket_events").fetchone()[0], 0)

    def test_status_is_rolled_back_when_event_insert_fails(self):
        ticket = create_ticket("虚构需求", self.fields)
        original = events.get_ticket_events(ticket["id"])
        self.reject_event_writes()
        with self.assertRaises(sqlite3.IntegrityError):
            change_ticket_status(ticket["id"], "已完成")
        self.assertEqual(db.get_ticket(ticket["id"]), ticket)
        self.assertEqual(events.get_ticket_events(ticket["id"]), original)

    def test_review_is_rolled_back_when_event_insert_fails(self):
        ticket = self.create_pending_review()
        original = events.get_ticket_events(ticket["id"])
        self.reject_event_writes()
        with self.assertRaises(sqlite3.IntegrityError):
            review_ticket(ticket["id"], self.fields)
        self.assertEqual(db.get_ticket(ticket["id"]), ticket)
        self.assertEqual(events.get_ticket_events(ticket["id"]), original)

    def test_business_insert_failure_does_not_leave_an_event(self):
        ticket = create_ticket("虚构需求", self.fields)
        original = events.get_ticket_events(ticket["id"])
        with self.assertRaises(sqlite3.IntegrityError):
            db.save_ticket(ticket)
        self.assertEqual(events.get_ticket_events(ticket["id"]), original)

    def test_events_require_a_transaction_and_an_existing_ticket(self):
        db.init_db()
        with db._connection() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            with self.assertRaises(ValueError):
                events.create_event(connection, "unknown", "ticket_created", "system", {}, {"status": "待确认"})
        with self.assertRaises(sqlite3.IntegrityError), db._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            events.create_event(connection, "unknown", "ticket_created", "system", {}, {"status": "待确认"})

    def test_duplicate_operation_id_rolls_back_business_change(self):
        ticket = create_ticket("虚构需求", self.fields)
        original = events.get_ticket_events(ticket["id"])
        with self.assertRaises(sqlite3.IntegrityError), db._connection() as connection:
            connection.execute("UPDATE tickets SET status = '已完成' WHERE id = ?", (ticket["id"],))
            events.create_event(
                connection, ticket["id"], "status_changed", "operator",
                {"status": "待确认"}, {"status": "已完成"},
                operation_id=original[0]["operation_id"],
            )
        self.assertEqual(db.get_ticket(ticket["id"]), ticket)
        self.assertEqual(events.get_ticket_events(ticket["id"]), original)

    def test_old_database_migration_preserves_ticket_without_fabricating_events(self):
        # 模拟 V1 已有工单、尚无事件表的数据库。
        ticket = create_ticket("历史虚构需求", self.fields)
        with db._connection() as connection:
            connection.execute("DROP TABLE ticket_events")
        db.init_db()
        db.init_db()
        self.assertEqual(db.get_ticket(ticket["id"]), ticket)
        self.assertEqual(events.get_ticket_events(ticket["id"]), [])
        with db._connection() as connection:
            columns = connection.execute("PRAGMA table_info(ticket_events)").fetchall()
            self.assertEqual([column[1] for column in columns], [
                "id", "ticket_id", "operation_id", "event_type", "actor_type",
                "before_data", "after_data", "reason", "created_at",
            ])
            self.assertEqual(len(connection.execute("PRAGMA table_info(tickets)").fetchall()), 23)
        change_ticket_status(ticket["id"], "已完成")
        self.assertEqual(len(events.get_ticket_events(ticket["id"])), 1)

    def test_concurrent_status_updates_have_a_consistent_before_after_chain(self):
        ticket = create_ticket("虚构需求", self.fields)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda status: change_ticket_status(ticket["id"], status), ["服务中", "异常"]))
        self.assertEqual(results, [True, True])
        history = events.get_ticket_events(ticket["id"])
        self.assertEqual(len(history), 4)
        history = [item for item in history if "status" in item["after_data"]]
        for previous, current in zip(history, history[1:]):
            self.assertEqual(previous["after_data"]["status"], current["before_data"]["status"])
        self.assertEqual(history[-1]["after_data"]["status"], db.get_ticket(ticket["id"])["status"])

    def test_seed_records_import_time_and_does_not_duplicate_events(self):
        started = datetime.now(timezone(timedelta(hours=8)))
        self.assertEqual(seed.seed_demo_data(), 120)
        for ticket in db.get_all_tickets():
            history = events.get_ticket_events(ticket["id"])
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["event_type"], "demo_imported")
            self.assertEqual(history[0]["actor_type"], "demo")
            self.assertGreaterEqual(datetime.fromisoformat(history[0]["created_at"]), started)
        self.assertEqual(seed.seed_demo_data(), 0)
        with db._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ticket_events").fetchone()[0], 120)
        self.sdk.assert_not_called()

    def test_seed_rolls_back_tickets_and_earlier_events_on_event_failure(self):
        db.init_db()
        with db._connection() as connection:
            connection.execute(
                """CREATE TRIGGER reject_second_event BEFORE INSERT ON ticket_events
                   WHEN (SELECT COUNT(*) FROM ticket_events) = 1
                   BEGIN SELECT RAISE(ABORT, 'test event failure'); END"""
            )
        with self.assertRaises(sqlite3.IntegrityError):
            seed.seed_demo_data()
        self.assertEqual(db.get_all_tickets(), [])
        with db._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ticket_events").fetchone()[0], 0)

    def test_page_shows_changes_and_refresh_does_not_create_events(self):
        first = create_ticket("第一条虚构需求", self.fields)
        second = create_ticket("第二条虚构需求", self.fields)
        change_ticket_status(first["id"], "服务中")
        app = self.open_app()
        selector = next(item for item in app.selectbox if item.label == "选择工单")
        selector.set_value(first["id"]).run(timeout=30)
        history_table = next(item.value for item in app.table if "事件类型" in item.value.columns)
        self.assertEqual(list(history_table.columns), ["时间", "事件类型", "操作来源", "变化内容"])
        self.assertEqual(list(history_table["事件类型"]), ["创建工单", "生成优先级建议", "状态修改"])
        self.assertEqual(history_table.iloc[-1]["操作来源"], "运营操作")
        self.assertIn("待确认 → 服务中", history_table.iloc[-1]["变化内容"])
        original = events.get_ticket_events(first["id"])
        app.run(timeout=30)
        self.assertEqual(events.get_ticket_events(first["id"]), original)
        next(item for item in app.selectbox if item.label == "选择工单").set_value(second["id"]).run(timeout=30)
        history_table = next(item.value for item in app.table if "事件类型" in item.value.columns)
        self.assertEqual(list(history_table["事件类型"]), ["创建工单", "生成优先级建议"])
        self.sdk.assert_not_called()

    def test_page_reports_unavailable_history_without_losing_ticket_management(self):
        create_ticket("虚构需求", self.fields)
        with patch.object(events, "get_ticket_events", side_effect=sqlite3.OperationalError("test")):
            app = self.open_app()
        self.assertTrue(any("操作历史暂不可读取" in item.value for item in app.warning))
        self.assertTrue(any(item.label == "保存状态" for item in app.button))
        self.sdk.assert_not_called()


if __name__ == "__main__":
    unittest.main()
