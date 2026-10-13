"""确认派单、并发保护、迁移和页面回归；仅使用临时数据库及虚构资料。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import os
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import config
from database import db
from modules import ai_client, ai_logging, ai_parser, dashboard, dispatch, events, staff


class DispatchTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self._patch(patch.object(db, "DB_PATH", self.directory / "autoflow.db"))
        self._patch(patch.object(ai_logging, "LOG_PATH", self.directory / "ai_calls.json"))
        self._patch(patch.object(config, "DEMO_MODE", True))
        self._patch(patch.dict(os.environ, {"OPENAI_API_KEY": ""}))
        self._patch(patch.object(ai_parser, "load_dotenv"))
        self.parser_sdk = self._patch(patch.object(ai_parser, "OpenAI"))
        self.client_sdk = self._patch(patch.object(ai_client, "OpenAI"))
        self.ticket = self.save_ticket()
        self.person = staff.create_staff("测试护理员（虚构）", skills=["老人护理"], service_area="演示甲区")

    def _patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def save_ticket(self, **changes):
        ticket = {
            "id": "dispatch-test", "customer_input": "虚构护理预约", "service_type": "老人护理",
            "address": "演示甲区示例小区", "appointment_time": "明天下午", "customer_name": "测试客户",
            "phone": "演示电话001", "status": "待确认", "created_time": "2026-10-14T10:00:00+08:00",
            "priority_final": "高", "priority_suggested": "高", **changes,
        }
        db.save_ticket(ticket)
        return db.get_ticket(ticket["id"])

    def create(self, **changes):
        return dispatch.create_dispatch(**{
            "ticket_id": self.ticket["id"], "staff_id": self.person["id"],
            "confirmed_by": "测试运营", "reason": "已人工核对需求与档期", **changes,
        })

    def snapshot(self):
        with db._connection() as connection:
            return "\n".join(connection.iterdump())

    def test_create_persists_all_fields_and_returns_id(self):
        dispatch_id = self.create()
        record = dispatch.get_ticket_dispatches(self.ticket["id"])[0]
        self.assertEqual(record["id"], dispatch_id)
        self.assertEqual(record["ticket_id"], self.ticket["id"])
        self.assertEqual(record["staff_id"], self.person["id"])
        self.assertEqual(record["status"], "confirmed")
        self.assertEqual(record["confirmed_by"], "测试运营")
        self.assertEqual(record["confirm_reason"], "已人工核对需求与档期")
        self.assertTrue(record["assigned_at"])
        self.assertEqual(record["assigned_at"], record["created_at"])
        self.assertEqual(record["is_demo"], 0)

    def test_duplicate_dispatch_rejected_without_extra_changes(self):
        self.create()
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "不能重复派单"):
            self.create(busy_confirmed=True)
        self.assertEqual(self.snapshot(), before)

    def test_missing_address_rejected(self):
        ticket = self.save_ticket(id="no-address", address="  ")
        with self.assertRaisesRegex(ValueError, "服务地址"):
            self.create(ticket_id=ticket["id"])

    def test_other_required_fields_rejected(self):
        for field in ("customer_name", "phone", "service_type", "appointment_time"):
            with self.subTest(field=field):
                ticket = self.save_ticket(id=field, **{field: ""})
                before = self.snapshot()
                with self.assertRaisesRegex(ValueError, "工单信息不完整"):
                    self.create(ticket_id=ticket["id"])
                self.assertEqual(self.snapshot(), before)

    def test_cancelled_or_completed_ticket_rejected(self):
        for status in ("已取消", "取消", "cancelled", "已完成"):
            with self.subTest(status=status):
                ticket = self.save_ticket(id=status, status=status)
                with self.assertRaisesRegex(ValueError, "禁止派单"):
                    self.create(ticket_id=ticket["id"])

    def test_unreviewed_ticket_rejected(self):
        for changes in ({"review_required": True}, {"status": "人工审核"}):
            ticket = self.save_ticket(id=str(changes), **changes)
            with self.assertRaisesRegex(ValueError, "人工审核"):
                self.create(ticket_id=ticket["id"])

    def test_nonexistent_ticket_or_staff_rejected(self):
        before = self.snapshot()
        for changes, message in (({"ticket_id": "missing"}, "工单不存在"), ({"staff_id": 999}, "人员不存在")):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                self.create(**changes)
        self.assertEqual(self.snapshot(), before)

    def test_inactive_staff_rejected(self):
        staff.update_staff_status(self.person["id"], "inactive")
        with self.assertRaisesRegex(ValueError, "人员已停用"):
            self.create(busy_confirmed=True)

    def test_busy_staff_requires_explicit_confirmation(self):
        staff.update_staff_status(self.person["id"], "busy")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "额外明确确认"):
            self.create(reason="只是填写原因不能代替确认")
        self.assertEqual(self.snapshot(), before)
        self.assertIsInstance(self.create(busy_confirmed=True), int)
        self.assertEqual(staff.get_staff(self.person["id"])["status"], "busy")
        event = events.get_ticket_events(self.ticket["id"])[-1]
        self.assertEqual(event["before_data"]["staff_status"], "busy")
        self.assertEqual(event["after_data"]["staff_status"], "busy")
        self.assertTrue(event["after_data"]["busy_confirmed"])

    def test_staff_becomes_busy_and_staff_history_records_transition(self):
        self.create()
        self.assertEqual(staff.get_staff(self.person["id"])["status"], "busy")
        event = events.get_staff_events(self.person["id"])[-1]
        self.assertEqual(event["event_type"], "staff_status_changed")
        self.assertEqual(event["before_data"], {"status": "available"})
        self.assertEqual(event["after_data"], {"status": "busy"})

    def test_ticket_events_link_dispatch_and_operator(self):
        dispatch_id = self.create()
        history = events.get_ticket_events(self.ticket["id"])[-2:]
        self.assertEqual([event["event_type"] for event in history], ["dispatch_created", "staff_assigned"])
        for event in history:
            self.assertEqual(event["actor_type"], "operator")
            self.assertEqual(event["after_data"]["dispatch_id"], dispatch_id)
            self.assertEqual(event["after_data"]["confirmed_by"], "测试运营")
            self.assertEqual(event["reason"], "已人工核对需求与档期")
        self.assertEqual(history[1]["before_data"]["staff_status"], "available")
        self.assertEqual(history[1]["after_data"]["staff_status"], "busy")
        self.assertNotEqual(history[0]["operation_id"], history[1]["operation_id"])

    def test_ticket_fields_priority_and_status_remain_unchanged(self):
        before = db.get_ticket(self.ticket["id"])
        self.create()
        self.assertEqual(db.get_ticket(self.ticket["id"]), before)
        self.parser_sdk.assert_not_called()
        self.client_sdk.assert_not_called()

    def test_invalid_arguments_and_blank_operator_reason_rejected(self):
        for changes in ({"confirmed_by": " "}, {"reason": " "}, {"staff_id": True},
                        {"staff_id": 0}, {"ticket_id": ""}, {"busy_confirmed": "yes"},
                        {"expected_snapshot": []}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.create(**changes)
        self.assertEqual(dispatch.get_ticket_dispatches(self.ticket["id"]), [])

    def test_unrelated_skills_rejected(self):
        person = staff.create_staff("测试保洁员", skills=["居家保洁"])
        with self.assertRaisesRegex(ValueError, "技能.*不匹配"):
            self.create(staff_id=person["id"])

    def test_partial_skills_and_unmatched_area_can_be_manually_confirmed(self):
        person = staff.create_staff("测试辅助人员", skills=["康复辅助"], service_area="演示乙区")
        self.assertIsInstance(self.create(staff_id=person["id"]), int)

    def test_demo_staff_and_demo_tickets_mark_dispatch(self):
        demo_person = staff.create_staff("演示人员", skills=["老人护理"], is_demo=True)
        self.create(staff_id=demo_person["id"])
        for changes in ({"id": "DEMO-example"}, {"id": "demo-source", "priority_source": "demo"}):
            ticket = self.save_ticket(**changes)
            self.create(ticket_id=ticket["id"], busy_confirmed=True)
            self.assertEqual(dispatch.get_ticket_dispatches(ticket["id"])[0]["is_demo"], 1)
        self.assertEqual(dispatch.get_ticket_dispatches(self.ticket["id"])[0]["is_demo"], 1)

    def test_stale_ticket_or_staff_snapshot_rejected(self):
        snapshot = dispatch.dispatch_snapshot(self.ticket, self.person)
        staff.update_staff(self.person["id"], service_area="演示乙区")
        with self.assertRaisesRegex(ValueError, "资料已变化"):
            self.create(expected_snapshot=snapshot)
        snapshot = dispatch.dispatch_snapshot(self.ticket, staff.get_staff(self.person["id"]))
        with db._connection() as connection:
            connection.execute("UPDATE tickets SET appointment_time = '后天下午' WHERE id = ?", (self.ticket["id"],))
        with self.assertRaisesRegex(ValueError, "资料已变化"):
            self.create(expected_snapshot=snapshot)

    def test_current_snapshot_allows_confirmation(self):
        self.assertIsInstance(self.create(expected_snapshot=dispatch.dispatch_snapshot(self.ticket, self.person)), int)

    def _assert_failure_rolls_back(self, table, predicate="1"):
        with db._connection() as connection:
            connection.execute(f"CREATE TRIGGER reject_dispatch BEFORE INSERT ON {table} WHEN {predicate} BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        before = self.snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            self.create()
        self.assertEqual(self.snapshot(), before)

    def test_dispatch_insert_failure_rolls_back(self):
        self._assert_failure_rolls_back("dispatches")

    def test_staff_event_failure_rolls_back_dispatch_and_state(self):
        self._assert_failure_rolls_back("staff_events", "NEW.event_type = 'staff_status_changed'")

    def test_first_ticket_event_failure_rolls_back_everything(self):
        self._assert_failure_rolls_back("ticket_events", "NEW.event_type = 'dispatch_created'")

    def test_second_ticket_event_failure_rolls_back_first_event_too(self):
        self._assert_failure_rolls_back("ticket_events", "NEW.event_type = 'staff_assigned'")

    def test_staff_update_failure_rolls_back_dispatch(self):
        with db._connection() as connection:
            connection.execute("CREATE TRIGGER reject_staff BEFORE UPDATE ON staff BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        before = self.snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            self.create()
        self.assertEqual(self.snapshot(), before)

    def test_database_unique_index_and_foreign_keys_protect_dispatches(self):
        self.create()
        with db._connection() as connection:
            for ticket_id, staff_id, status in ((self.ticket["id"], self.person["id"], "confirmed"),
                                                ("unknown", self.person["id"], "confirmed"),
                                                (self.ticket["id"], 999, "cancelled"),
                                                (self.ticket["id"], self.person["id"], "invalid")):
                with self.subTest(ticket_id=ticket_id, staff_id=staff_id, status=status), self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("INSERT INTO dispatches(ticket_id, staff_id, status) VALUES (?, ?, ?)", (ticket_id, staff_id, status))

    def test_cancelled_history_does_not_block_new_confirmed_dispatch(self):
        with db._connection() as connection:
            connection.execute("INSERT INTO dispatches(ticket_id, staff_id, status) VALUES (?, ?, 'cancelled')", (self.ticket["id"], self.person["id"]))
        self.create()
        self.assertEqual([row["status"] for row in dispatch.get_ticket_dispatches(self.ticket["id"])], ["confirmed", "cancelled"])

    def test_concurrent_same_ticket_creates_only_one_dispatch(self):
        other = staff.create_staff("测试候选乙", skills=["老人护理"])
        def attempt(person_id):
            try:
                return self.create(staff_id=person_id)
            except ValueError as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, [self.person["id"], other["id"]]))
        self.assertEqual(sum(type(result) is int for result in results), 1)
        self.assertTrue(any("不能重复派单" in str(result) for result in results))
        self.assertEqual(sum(person["status"] == "busy" for person in staff.list_staff()), 1)

    def test_concurrent_same_staff_requires_busy_confirmation_for_second_ticket(self):
        second = self.save_ticket(id="second")
        def attempt(ticket_id):
            try:
                return self.create(ticket_id=ticket_id)
            except ValueError as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, [self.ticket["id"], second["id"]]))
        self.assertEqual(sum(type(result) is int for result in results), 1)
        self.assertTrue(any("额外明确确认" in str(result) for result in results))

    def test_metrics_read_dispatch_table_including_cancelled_and_demo(self):
        self.assertEqual(dispatch.get_dispatch_metrics(), {
            "dispatch_total_count": 0, "dispatch_confirmed_count": 0,
            "dispatch_cancelled_count": 0, "dispatch_demo_count": 0,
        })
        self.create()
        with db._connection() as connection:
            connection.execute("INSERT INTO dispatches(ticket_id, staff_id, status, is_demo) VALUES (?, ?, 'cancelled', 1)", (self.ticket["id"], self.person["id"]))
        stats = dashboard.get_dashboard_stats()
        self.assertEqual(stats["dispatch_total_count"], 2)
        self.assertEqual(stats["dispatch_confirmed_count"], 1)
        self.assertEqual(stats["dispatch_cancelled_count"], 1)
        self.assertEqual(stats["dispatch_demo_count"], 1)
        self.assertEqual(stats["ai_call_count"], 0)

    def test_old_database_migration_preserves_rows_and_does_not_backfill(self):
        old_path = self.directory / "old.db"
        with closing(sqlite3.connect(old_path)) as connection, connection:
            connection.execute("CREATE TABLE tickets(id TEXT PRIMARY KEY, customer_input TEXT, service_type TEXT, appointment_time TEXT, address TEXT, customer_name TEXT, phone TEXT, urgency TEXT, status TEXT, created_time TEXT)")
            connection.execute("INSERT INTO tickets VALUES ('old', '历史虚构需求', '', '', '', '', '', '', '待确认', '2026-10-01')")
            connection.execute("CREATE TABLE staff(id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT, skills TEXT, service_area TEXT, status TEXT, created_at TEXT)")
            connection.execute("INSERT INTO staff VALUES (7, '历史虚构人员', '', '[]', '', 'available', '2026-10-01')")
            original_ticket = connection.execute("SELECT * FROM tickets").fetchone()
            original_staff = connection.execute("SELECT * FROM staff").fetchone()
        with patch.object(db, "DB_PATH", old_path):
            db.init_db()
            before = self.snapshot()
            db.init_db()
            self.assertEqual(self.snapshot(), before)
            with db._connection() as connection:
                self.assertEqual(tuple(connection.execute("SELECT * FROM tickets").fetchone())[:10], original_ticket)
                self.assertEqual(tuple(connection.execute("SELECT * FROM staff").fetchone())[:7], original_staff)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM ticket_events").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM staff_events").fetchone()[0], 0)
                self.assertEqual(len(connection.execute("PRAGMA table_info(dispatches)").fetchall()), 14)
                indexes = {row["name"] for row in connection.execute("PRAGMA index_list(dispatches)")}
                self.assertTrue({"idx_dispatches_ticket", "idx_dispatches_staff", "idx_dispatches_one_effective"} <= indexes)

    def open_app(self):
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
        self.assertFalse(app.exception)
        return app

    def click(self, app, label):
        next(item for item in app.button if item.label == label).click().run(timeout=30)
        self.assertFalse(app.exception)

    def open_confirmation(self, app):
        self.click(app, "查看推荐")
        self.click(app, "确认派单")

    def fill_confirmation(self, app, *, busy=False):
        next(item for item in app.text_input if item.label == "确认人").set_value("测试运营")
        next(item for item in app.text_area if item.label == "派单原因").set_value("人工已核对需求与时间")
        next(item for item in app.checkbox if item.label == "我已核对服务需求、服务范围和预约时间").check()
        if busy:
            next(item for item in app.checkbox if item.label == "我已确认该忙碌人员可以承接本单").check()

    def test_page_view_explain_open_cancel_and_refresh_do_not_dispatch(self):
        before = self.snapshot()
        app = self.open_app()
        self.click(app, "生成AI推荐说明")
        self.click(app, "确认派单")
        self.assertTrue(any(item.label == "确认人" for item in app.text_input))
        self.assertEqual(self.snapshot(), before)
        self.click(app, "取消本次确认")
        app.run(timeout=30)
        self.assertFalse(app.exception)
        self.assertEqual(self.snapshot(), before)
        self.parser_sdk.assert_not_called()
        self.client_sdk.assert_not_called()

    def test_page_confirmation_saves_and_displays_history_metrics_and_id(self):
        app = self.open_app()
        self.open_confirmation(app)
        self.fill_confirmation(app)
        self.click(app, "确认并创建派单")
        self.assertTrue(any("派单成功，派单编号：1" in item.value for item in app.success))
        self.assertTrue(any("已确认派单 #1" in item.value for item in app.info))
        self.assertEqual(next(item.value for item in app.metric if item.label == "已确认派单数量"), "1")
        self.assertTrue(any("关联服务人员" in table.value.to_string() for table in app.table))
        app.run(timeout=30)
        self.click(app, "查看推荐")
        self.assertTrue(next(item for item in app.button if item.label == "确认派单").disabled)
        self.assertEqual(len(dispatch.get_ticket_dispatches(self.ticket["id"])), 1)

    def test_page_requires_operator_reason_and_verification(self):
        app = self.open_app()
        self.open_confirmation(app)
        self.click(app, "确认并创建派单")
        self.assertTrue(any("请先核对" in item.value for item in app.error))
        next(item for item in app.checkbox if item.label == "我已核对服务需求、服务范围和预约时间").check()
        self.click(app, "确认并创建派单")
        self.assertTrue(any("确认人不能为空" in item.value for item in app.error))
        next(item for item in app.text_input if item.label == "确认人").set_value("测试运营")
        self.click(app, "确认并创建派单")
        self.assertTrue(any("派单原因不能为空" in item.value for item in app.error))
        self.assertEqual(dispatch.get_ticket_dispatches(self.ticket["id"]), [])

    def test_page_busy_staff_requires_extra_checkbox(self):
        staff.update_staff_status(self.person["id"], "busy")
        app = self.open_app()
        self.open_confirmation(app)
        self.fill_confirmation(app)
        self.click(app, "确认并创建派单")
        self.assertTrue(any("额外明确确认" in item.value for item in app.error))
        self.fill_confirmation(app, busy=True)
        self.click(app, "确认并创建派单")
        self.assertEqual(len(dispatch.get_ticket_dispatches(self.ticket["id"])), 1)

    def test_page_stale_form_rejected_and_no_side_effects(self):
        app = self.open_app()
        self.open_confirmation(app)
        self.fill_confirmation(app)
        staff.update_staff(self.person["id"], service_area="演示乙区")
        before = self.snapshot()
        self.click(app, "确认并创建派单")
        self.assertTrue(any("资料已变化" in item.value for item in app.error))
        self.assertEqual(self.snapshot(), before)

    def test_page_database_error_shows_failure_without_partial_dispatch(self):
        app = self.open_app()
        self.open_confirmation(app)
        self.fill_confirmation(app)
        with patch.object(events, "create_event", side_effect=sqlite3.OperationalError("test error")):
            self.click(app, "确认并创建派单")
        self.assertTrue(any("派单保存失败" in item.value for item in app.error))
        self.assertEqual(dispatch.get_ticket_dispatches(self.ticket["id"]), [])
        self.assertEqual(staff.get_staff(self.person["id"])["status"], "available")


if __name__ == "__main__":
    unittest.main()
