"""服务状态机、事务、旧库迁移与运营页面；全部使用临时库和虚构信息。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import config
from database import db
from modules import ai_client, ai_logging, ai_parser, dashboard, dispatch, events, service_tracking as tracking, staff


class ServiceTrackingTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self._patch(patch.object(db, "DB_PATH", Path(directory.name) / "test.db"))
        self._patch(patch.object(ai_logging, "LOG_PATH", Path(directory.name) / "log.json"))
        self._patch(patch.object(config, "DEMO_MODE", True))
        self._patch(patch.dict(os.environ, {"OPENAI_API_KEY": ""}))
        self._patch(patch.object(ai_parser, "load_dotenv"))
        self.parser = self._patch(patch.object(ai_parser, "OpenAI"))
        self.client = self._patch(patch.object(ai_client, "OpenAI"))
        self.ticket = {
            "id": "tracking-test", "customer_input": "虚构护理预约", "service_type": "老人护理",
            "customer_name": "测试客户", "phone": "演示电话001", "address": "演示甲区",
            "appointment_time": "明天下午", "status": "待确认", "priority_final": "高",
            "created_time": "2026-10-14T09:00:00+08:00",
        }
        db.save_ticket(self.ticket)
        self.person = staff.create_staff("虚构护理员", skills=["老人护理"], service_area="演示甲区")
        self.dispatch_id = dispatch.create_dispatch(self.ticket["id"], self.person["id"], "测试运营", "确认资料")

    def _patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def record(self, dispatch_id=None):
        with db._connection() as connection:
            return dict(connection.execute("SELECT * FROM dispatches WHERE id = ?", (dispatch_id or self.dispatch_id,)).fetchone())

    def snapshot(self):
        with db._connection() as connection:
            return "\n".join(connection.iterdump())

    def progress(self, status, dispatch_id=None):
        dispatch_id = dispatch_id or self.dispatch_id
        if status in ("accepted", "in_service", "completed", "exception"):
            tracking.accept_dispatch(dispatch_id)
        if status in ("in_service", "completed"):
            tracking.start_service(dispatch_id)
        if status == "completed":
            tracking.complete_service(dispatch_id, "测试服务完成")
        if status == "exception":
            tracking.mark_exception(dispatch_id, "测试服务暂停")
        if status == "cancelled":
            tracking.cancel_dispatch(dispatch_id, "测试取消")

    def extra_dispatch(self, suffix):
        ticket = {**self.ticket, "id": suffix}
        db.save_ticket(ticket)
        return dispatch.create_dispatch(ticket["id"], self.person["id"], "测试运营", "人工确认多单", busy_confirmed=True)

    def test_normal_flow_records_three_timestamps_and_note(self):
        moments = ["2026-10-14T10:00:00+08:00", "2026-10-14T11:00:00+08:00", "2026-10-14T12:30:00+08:00"]
        with patch.object(db, "_now_text", side_effect=moments):
            accepted = tracking.accept_dispatch(self.dispatch_id)
            started = tracking.start_service(self.dispatch_id)
            completed = tracking.complete_service(self.dispatch_id, "  已核对完成  ")
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["accepted_at"], moments[0])
        self.assertIsNone(accepted["started_at"])
        self.assertEqual(started["status"], "in_service")
        self.assertEqual(started["started_at"], moments[1])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["completed_at"], moments[2])
        self.assertEqual(completed["accepted_at"], moments[0])
        self.assertEqual(completed["completed_note"], "已核对完成")

    def test_all_invalid_state_transitions_leave_database_unchanged(self):
        allowed = {
            "confirmed": {"accepted", "cancelled"}, "accepted": {"in_service", "exception", "cancelled"},
            "in_service": {"completed", "exception", "cancelled"}, "exception": {"cancelled"},
            "completed": set(), "cancelled": set(),
        }
        operations = {
            "accepted": lambda: tracking.accept_dispatch(self.dispatch_id),
            "in_service": lambda: tracking.start_service(self.dispatch_id),
            "completed": lambda: tracking.complete_service(self.dispatch_id, "完成"),
            "exception": lambda: tracking.mark_exception(self.dispatch_id, "异常"),
            "cancelled": lambda: tracking.cancel_dispatch(self.dispatch_id, "取消"),
        }
        for origin, targets in allowed.items():
            with db._connection() as connection:
                connection.execute("UPDATE dispatches SET status = ? WHERE id = ?", (origin, self.dispatch_id))
            for target, operation in operations.items():
                if target in targets:
                    continue
                with self.subTest(origin=origin, target=target):
                    before = self.snapshot()
                    with self.assertRaisesRegex(ValueError, "非法状态流转"):
                        operation()
                    self.assertEqual(self.snapshot(), before)

    def test_missing_dispatch_and_invalid_ids_cannot_start(self):
        before = self.snapshot()
        for dispatch_id in (999, 0, -1, True, "1", None):
            with self.subTest(dispatch_id=dispatch_id), self.assertRaises(ValueError):
                tracking.start_service(dispatch_id)
        self.assertEqual(self.snapshot(), before)

    def test_completion_releases_busy_staff_and_records_staff_event(self):
        self.progress("completed")
        self.assertEqual(staff.get_staff(self.person["id"])["status"], "available")
        event = events.get_staff_events(self.person["id"])[-1]
        self.assertEqual(event["event_type"], "staff_status_changed")
        self.assertEqual(event["before_data"], {"status": "busy"})
        self.assertEqual(event["after_data"], {"status": "available"})

    def test_completion_with_other_open_dispatch_keeps_staff_busy(self):
        other = self.extra_dispatch("other")
        for other_status in ("confirmed", "accepted", "in_service", "exception"):
            with self.subTest(other_status=other_status):
                with db._connection() as connection:
                    connection.execute("UPDATE dispatches SET status = 'in_service' WHERE id = ?", (self.dispatch_id,))
                    connection.execute("UPDATE dispatches SET status = ? WHERE id = ?", (other_status, other))
                tracking.complete_service(self.dispatch_id, "本单完成")
                self.assertEqual(staff.get_staff(self.person["id"])["status"], "busy")

    def test_last_of_multiple_services_releases_staff(self):
        other = self.extra_dispatch("other")
        self.progress("completed")
        self.assertEqual(staff.get_staff(self.person["id"])["status"], "busy")
        self.progress("completed", other)
        self.assertEqual(staff.get_staff(self.person["id"])["status"], "available")

    def test_cancelled_other_dispatch_does_not_block_completion_release(self):
        other = self.extra_dispatch("other")
        tracking.cancel_dispatch(other, "取消另一单")
        self.assertEqual(staff.get_staff(self.person["id"])["status"], "busy")
        self.progress("completed")
        self.assertEqual(staff.get_staff(self.person["id"])["status"], "available")

    def test_completion_does_not_reactivate_inactive_staff(self):
        self.progress("in_service")
        staff.update_staff_status(self.person["id"], "inactive")
        tracking.complete_service(self.dispatch_id, "登记已完成事实")
        self.assertEqual(staff.get_staff(self.person["id"])["status"], "inactive")

    def test_inactive_staff_cannot_accept_or_start(self):
        staff.update_staff_status(self.person["id"], "inactive")
        with self.assertRaisesRegex(ValueError, "停用"):
            tracking.accept_dispatch(self.dispatch_id)
        staff.update_staff_status(self.person["id"], "busy")
        tracking.accept_dispatch(self.dispatch_id)
        staff.update_staff_status(self.person["id"], "inactive")
        with self.assertRaisesRegex(ValueError, "停用"):
            tracking.start_service(self.dispatch_id)

    def test_exception_from_accepted_keeps_busy_and_stores_reason(self):
        self.progress("accepted")
        updated = tracking.mark_exception(self.dispatch_id, "  联系不上客户  ")
        self.assertEqual(updated["status"], "exception")
        self.assertEqual(updated["exception_reason"], "联系不上客户")
        self.assertIsNone(updated["started_at"])
        self.assertIsNone(updated["completed_at"])
        self.assertEqual(staff.get_staff(self.person["id"])["status"], "busy")

    def test_exception_from_in_service_preserves_times_and_cannot_complete(self):
        self.progress("in_service")
        before = self.record()
        tracking.mark_exception(self.dispatch_id, "现场条件不满足")
        updated = self.record()
        self.assertEqual(updated["accepted_at"], before["accepted_at"])
        self.assertEqual(updated["started_at"], before["started_at"])
        with self.assertRaisesRegex(ValueError, "非法状态流转"):
            tracking.complete_service(self.dispatch_id, "跳过异常")

    def test_blank_exception_or_cancel_reason_rejected(self):
        self.progress("accepted")
        for action in (tracking.mark_exception, tracking.cancel_dispatch):
            for reason in ("", " ", None, 123):
                with self.subTest(action=action.__name__, reason=reason), self.assertRaises(ValueError):
                    action(self.dispatch_id, reason)
        self.assertEqual(self.record()["status"], "accepted")

    def test_completion_empty_note_allowed_but_non_text_rejected(self):
        self.progress("in_service")
        with self.assertRaises(ValueError):
            tracking.complete_service(self.dispatch_id, None)
        result = tracking.complete_service(self.dispatch_id, "")
        self.assertEqual(result["completed_note"], "")
        self.assertTrue(events.get_ticket_events(self.ticket["id"])[-1]["reason"])

    def test_cancel_allowed_from_every_unfinished_state_without_release(self):
        for status in ("confirmed", "accepted", "in_service", "exception"):
            with self.subTest(status=status):
                with db._connection() as connection:
                    connection.execute("UPDATE dispatches SET status = ? WHERE id = ?", (status, self.dispatch_id))
                self.assertEqual(tracking.cancel_dispatch(self.dispatch_id, "客户取消")["status"], "cancelled")
                self.assertEqual(staff.get_staff(self.person["id"])["status"], "busy")

    def test_events_contain_identity_before_after_times_and_reason(self):
        self.progress("completed")
        history = events.get_ticket_events(self.ticket["id"])[-3:]
        self.assertEqual([event["event_type"] for event in history], ["dispatch_accepted", "service_started", "service_completed"])
        for event, (origin, target, time_field) in zip(history, (
            ("confirmed", "accepted", "accepted_at"), ("accepted", "in_service", "started_at"),
            ("in_service", "completed", "completed_at"),
        )):
            self.assertEqual(event["before_data"]["dispatch_status"], origin)
            self.assertEqual(event["after_data"]["dispatch_status"], target)
            self.assertEqual(event["before_data"]["dispatch_id"], self.dispatch_id)
            self.assertEqual(event["after_data"]["staff_id"], self.person["id"])
            self.assertIsNone(event["before_data"][time_field])
            self.assertTrue(event["after_data"][time_field])
            self.assertTrue(event["reason"])
            self.assertEqual(event["actor_type"], "operator")
        self.assertEqual(history[-1]["after_data"]["staff_status"], "available")

    def test_exception_and_cancel_events_keep_operational_reasons(self):
        self.progress("accepted")
        tracking.mark_exception(self.dispatch_id, "现场异常")
        tracking.cancel_dispatch(self.dispatch_id, "协商取消")
        history = events.get_ticket_events(self.ticket["id"])[-2:]
        self.assertEqual([item["event_type"] for item in history], ["service_exception", "dispatch_cancelled"])
        self.assertEqual([item["reason"] for item in history], ["现场异常", "协商取消"])
        self.assertEqual(history[0]["after_data"]["exception_reason"], "现场异常")

    def test_ticket_data_and_ai_calls_remain_unchanged(self):
        before = db.get_ticket(self.ticket["id"])
        self.progress("completed")
        self.assertEqual(db.get_ticket(self.ticket["id"]), before)
        self.parser.assert_not_called()
        self.client.assert_not_called()

    def test_duplicate_dispatch_blocked_after_accept_start_exception_or_completion(self):
        for status in ("accepted", "in_service", "exception", "completed"):
            with self.subTest(status=status):
                with db._connection() as connection:
                    connection.execute("UPDATE dispatches SET status = ? WHERE id = ?", (status, self.dispatch_id))
                with self.assertRaisesRegex(ValueError, "不能重复派单"):
                    dispatch.create_dispatch(self.ticket["id"], self.person["id"], "运营", "重复", busy_confirmed=True)
                with db._connection() as connection, self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("INSERT INTO dispatches(ticket_id, staff_id, status) VALUES (?, ?, 'confirmed')", (self.ticket["id"], self.person["id"]))

    def test_new_dispatch_after_cancel_keeps_old_progress_and_events(self):
        self.progress("exception")
        original = self.record()
        tracking.cancel_dispatch(self.dispatch_id, "重新安排")
        new_id = dispatch.create_dispatch(self.ticket["id"], self.person["id"], "运营", "重新确认", busy_confirmed=True)
        self.assertNotEqual(new_id, self.dispatch_id)
        self.assertEqual(self.record()["exception_reason"], original["exception_reason"])
        self.assertIsNone(self.record(new_id)["accepted_at"])

    def test_each_progress_event_failure_rolls_back(self):
        for origin, event_type, action in (
            ("confirmed", "dispatch_accepted", lambda: tracking.accept_dispatch(self.dispatch_id)),
            ("accepted", "service_started", lambda: tracking.start_service(self.dispatch_id)),
            ("in_service", "service_completed", lambda: tracking.complete_service(self.dispatch_id, "完成")),
            ("accepted", "service_exception", lambda: tracking.mark_exception(self.dispatch_id, "异常")),
            ("confirmed", "dispatch_cancelled", lambda: tracking.cancel_dispatch(self.dispatch_id, "取消")),
        ):
            with self.subTest(event_type=event_type):
                with db._connection() as connection:
                    connection.execute("UPDATE dispatches SET status = ? WHERE id = ?", (origin, self.dispatch_id))
                    connection.execute(f"CREATE TRIGGER reject_progress BEFORE INSERT ON ticket_events WHEN NEW.event_type = '{event_type}' BEGIN SELECT RAISE(ABORT, 'test failure'); END")
                before = self.snapshot()
                with self.assertRaises(sqlite3.IntegrityError):
                    action()
                self.assertEqual(self.snapshot(), before)
                with db._connection() as connection:
                    connection.execute("DROP TRIGGER reject_progress")

    def test_staff_event_failure_rolls_back_completion_and_release(self):
        self.progress("in_service")
        before = self.snapshot()
        with patch.object(events, "create_staff_event", side_effect=sqlite3.OperationalError("test failure")):
            with self.assertRaises(sqlite3.OperationalError):
                tracking.complete_service(self.dispatch_id, "完成")
        self.assertEqual(self.snapshot(), before)

    def test_staff_update_failure_rolls_back_completion(self):
        self.progress("in_service")
        with db._connection() as connection:
            connection.execute("CREATE TRIGGER reject_release BEFORE UPDATE ON staff BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        before = self.snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            tracking.complete_service(self.dispatch_id, "完成")
        self.assertEqual(self.snapshot(), before)

    def test_dispatch_update_failure_leaves_times_and_events_unchanged(self):
        with db._connection() as connection:
            connection.execute("CREATE TRIGGER reject_update BEFORE UPDATE ON dispatches BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        before = self.snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            tracking.accept_dispatch(self.dispatch_id)
        self.assertEqual(self.snapshot(), before)

    def test_concurrent_completion_records_once(self):
        self.progress("in_service")
        def attempt(_):
            try:
                return tracking.complete_service(self.dispatch_id, "完成")["status"]
            except ValueError:
                return "rejected"
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(attempt, range(2))), ["completed", "rejected"])
        history = events.get_ticket_events(self.ticket["id"])
        self.assertEqual(sum(item["event_type"] == "service_completed" for item in history), 1)

    def test_concurrent_multiple_completions_release_only_at_end(self):
        other = self.extra_dispatch("other")
        self.progress("in_service")
        self.progress("in_service", other)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda item: tracking.complete_service(item, "完成"), [self.dispatch_id, other]))
        self.assertTrue(all(item["status"] == "completed" for item in results))
        self.assertEqual(staff.get_staff(self.person["id"])["status"], "available")
        history = events.get_staff_events(self.person["id"])
        self.assertEqual(sum(item["after_data"].get("status") == "available" for item in history[1:]), 1)

    def test_metrics_compute_seconds_and_counts_from_dispatches(self):
        other = self.extra_dispatch("other")
        with db._connection() as connection:
            connection.execute("UPDATE dispatches SET status='completed', assigned_at='2026-10-14T09:00:00+08:00', accepted_at='2026-10-14T09:10:00+08:00', started_at='2026-10-14T10:00:00+08:00', completed_at='2026-10-14T11:30:00+08:00' WHERE id=?", (self.dispatch_id,))
            connection.execute("UPDATE dispatches SET status='exception', assigned_at='2026-10-14T09:00:00+08:00', accepted_at='2026-10-14T09:20:00+08:00' WHERE id=?", (other,))
        stats = tracking.get_service_metrics()
        self.assertEqual(stats, {"service_completed_count": 1, "service_exception_count": 1, "avg_acceptance_seconds": 900.0, "avg_service_seconds": 5400.0})
        self.assertEqual(dashboard.get_dashboard_stats()["service_completed_count"], 1)

    def test_metrics_empty_durations_are_none_not_zero(self):
        stats = tracking.get_service_metrics()
        self.assertEqual(stats["service_completed_count"], 0)
        self.assertIsNone(stats["avg_acceptance_seconds"])
        self.assertIsNone(stats["avg_service_seconds"])

    def test_metrics_skip_invalid_negative_and_unfinished_service_duration(self):
        with db._connection() as connection:
            connection.execute("UPDATE dispatches SET status='exception', assigned_at='bad date', accepted_at='2026-10-14T09:20:00+08:00', started_at='2026-10-14T09:00:00+08:00', completed_at='2026-10-14T10:00:00+08:00'")
        self.assertIsNone(tracking.get_service_metrics()["avg_acceptance_seconds"])
        self.assertIsNone(tracking.get_service_metrics()["avg_service_seconds"])
        with db._connection() as connection:
            connection.execute("UPDATE dispatches SET status='completed', assigned_at='2026-10-14T11:00:00+08:00', started_at='2026-10-14T12:00:00+08:00'")
        self.assertIsNone(tracking.get_service_metrics()["avg_acceptance_seconds"])
        self.assertIsNone(tracking.get_service_metrics()["avg_service_seconds"])

    def test_metrics_timezone_conversion_and_zero_duration(self):
        with db._connection() as connection:
            connection.execute("UPDATE dispatches SET status='completed', assigned_at='2026-10-14T09:00:00', accepted_at='2026-10-14T01:10:00+00:00', started_at='2026-10-14T10:00:00+08:00', completed_at='2026-10-14T02:00:00+00:00'")
        self.assertEqual(tracking.get_service_metrics()["avg_acceptance_seconds"], 600)
        self.assertEqual(tracking.get_service_metrics()["avg_service_seconds"], 0)

    def make_legacy_dispatch_table(self):
        # 用当前其他业务表模拟真实 4C-1 数据库，保留派单编号及原有事件。
        with db._connection() as connection:
            connection.execute("ALTER TABLE dispatches RENAME TO new_dispatches")
            connection.execute("CREATE TABLE dispatches(id INTEGER PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id), staff_id INTEGER NOT NULL REFERENCES staff(id), status TEXT NOT NULL CHECK(status IN ('confirmed', 'cancelled')), assigned_at TEXT, confirmed_by TEXT, confirm_reason TEXT, created_at TEXT, is_demo INTEGER DEFAULT 0)")
            connection.execute("INSERT INTO dispatches SELECT id,ticket_id,staff_id,status,assigned_at,confirmed_by,confirm_reason,created_at,is_demo FROM new_dispatches")
            connection.execute("DROP TABLE new_dispatches")
            connection.execute("CREATE UNIQUE INDEX idx_dispatches_one_confirmed ON dispatches(ticket_id) WHERE status='confirmed'")
            connection.execute("CREATE INDEX test_operator_index ON dispatches(confirmed_by)")
            connection.execute("CREATE TRIGGER preserve_reason BEFORE UPDATE ON dispatches WHEN NEW.confirm_reason = 'forbidden' BEGIN SELECT RAISE(ABORT, 'test protection'); END")

    def test_legacy_migration_preserves_data_events_indexes_and_triggers(self):
        self.make_legacy_dispatch_table()
        with db._connection() as connection:
            old = tuple(connection.execute("SELECT * FROM dispatches").fetchone())
            old_events = [tuple(row) for row in connection.execute("SELECT * FROM ticket_events")]
        db.init_db()
        with db._connection() as connection:
            migrated = tuple(connection.execute("SELECT * FROM dispatches").fetchone())
            self.assertEqual(migrated[:9], old)
            self.assertEqual(migrated[9:], (None,) * 5)
            self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM ticket_events")], old_events)
            names = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master")}
            self.assertTrue({"test_operator_index", "preserve_reason", "idx_dispatches_one_effective", "idx_dispatches_staff", "idx_dispatches_ticket"} <= names)
            self.assertNotIn("idx_dispatches_one_confirmed", names)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        before = self.snapshot()
        db.init_db()
        self.assertEqual(self.snapshot(), before)
        self.progress("completed")
        with db._connection() as connection, self.assertRaises(sqlite3.IntegrityError):
            connection.execute("UPDATE dispatches SET confirm_reason = 'forbidden'")

    def test_migration_failure_rolls_back_old_schema_and_data(self):
        self.make_legacy_dispatch_table()
        before = self.snapshot()
        original = db._migrate_dispatches
        def fail_after_rebuild(connection):
            original(connection)
            raise sqlite3.OperationalError("test migration failure")
        with patch.object(db, "_migrate_dispatches", side_effect=fail_after_rebuild), self.assertRaises(sqlite3.OperationalError):
            db.init_db()
        self.assertEqual(self.snapshot(), before)

    def open_app(self):
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
        self.assertFalse(app.exception)
        return app

    def click(self, app, label):
        next(item for item in app.button if item.label == label).click().run(timeout=30)
        self.assertFalse(app.exception)

    def test_page_normal_flow_buttons_timeline_metrics_and_refresh(self):
        app = self.open_app()
        self.assertTrue(any(item.label == "确认接单" for item in app.button))
        self.assertFalse(any(item.label == "完成服务" for item in app.button))
        self.click(app, "确认接单")
        self.assertTrue(any(item.label == "开始服务" for item in app.button))
        self.assertTrue(any(item.label == "标记异常" for item in app.button))
        self.click(app, "开始服务")
        next(item for item in app.text_area if item.label == "完成备注").set_value("现场服务完成")
        self.click(app, "完成服务")
        self.assertTrue(any("当前状态：已完成" in item.value for item in app.markdown))
        self.assertTrue(any("现场服务完成" in item.value for item in app.markdown))
        self.assertFalse(any(item.label in ("确认接单", "开始服务", "完成服务", "标记异常", "取消派单") for item in app.button))
        self.assertEqual(next(item.value for item in app.metric if item.label == "已完成服务数量"), "1")
        self.assertEqual(next(item.value for item in app.metric if item.label == "可用人员数量"), "1")
        timeline = next(table.value for table in app.table if "节点" in table.value.columns)
        self.assertEqual(len(timeline), 4)
        self.assertNotIn("未记录", list(timeline["登记时间"]))
        before = self.snapshot()
        app.run(timeout=30)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(db.get_ticket(self.ticket["id"])["status"], "待确认")
        self.client.assert_not_called()

    def test_page_exception_requires_reason_and_shows_it(self):
        self.progress("accepted")
        app = self.open_app()
        self.click(app, "标记异常")
        self.assertTrue(any("操作原因不能为空" in item.value for item in app.error))
        next(item for item in app.text_area if item.label == "异常原因").set_value("客户临时无法接待")
        self.click(app, "标记异常")
        self.assertTrue(any("客户临时无法接待" in item.value for item in app.warning))
        self.assertFalse(any(item.label == "完成服务" for item in app.button))
        self.assertEqual(next(item.value for item in app.metric if item.label == "异常服务数量"), "1")

    def test_page_cancellation_requires_explicit_confirmation(self):
        app = self.open_app()
        self.click(app, "取消派单")
        self.assertTrue(any("请先勾选" in item.value for item in app.error))
        next(item for item in app.checkbox if item.label == "我确认取消此派单").check()
        next(item for item in app.text_area if item.label == "取消原因").set_value("客户取消")
        self.click(app, "取消派单")
        self.assertEqual(self.record()["status"], "cancelled")
        self.assertEqual(staff.get_staff(self.person["id"])["status"], "busy")

    def test_page_missing_dispatch_has_no_service_action_and_empty_durations(self):
        other_ticket = {**self.ticket, "id": "no-dispatch"}
        db.save_ticket(other_ticket)
        app = self.open_app()
        next(item for item in app.selectbox if item.label == "选择工单").set_value("no-dispatch").run(timeout=30)
        self.assertFalse(app.exception)
        self.assertTrue(any("暂无派单记录" in item.value for item in app.info))
        self.assertFalse(any(item.label in ("确认接单", "开始服务", "完成服务") for item in app.button))
        self.assertEqual(next(item.value for item in app.metric if item.label == "平均接单时间"), "暂无数据")

    def test_page_error_rolls_back_and_does_not_claim_success(self):
        self.progress("in_service")
        app = self.open_app()
        before = self.snapshot()
        with patch.object(events, "create_event", side_effect=sqlite3.OperationalError("test failure")):
            self.click(app, "完成服务")
        self.assertTrue(any("服务进度保存失败" in item.value for item in app.error))
        self.assertEqual(self.snapshot(), before)

    def test_page_stale_action_cannot_overwrite_new_state(self):
        app = self.open_app()
        tracking.accept_dispatch(self.dispatch_id)
        self.click(app, "确认接单")
        self.assertEqual(self.record()["status"], "accepted")
        history = events.get_ticket_events(self.ticket["id"])
        self.assertEqual(sum(item["event_type"] == "dispatch_accepted" for item in history), 1)


if __name__ == "__main__":
    unittest.main()
