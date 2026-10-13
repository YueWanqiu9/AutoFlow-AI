"""人员资源池、独立事件、迁移和页面测试；仅使用临时数据库与虚构资料。"""

import json
import os
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import config
from database import db
from database.seed_staff import seed_demo_staff
from modules import ai_client, ai_logging, ai_parser, dashboard, events, staff


class StaffTests(unittest.TestCase):
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

    def _patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def add_staff(self, **kwargs):
        return staff.create_staff(**{
            "name": "测试护理员（虚构）", "skills": ["老人护理", "助浴"],
            "phone": "", "service_area": "演示城区", **kwargs,
        })

    def event_count(self):
        with db._connection() as connection:
            return connection.execute("SELECT COUNT(*) FROM staff_events").fetchone()[0]

    def reject_events(self, event_type=None):
        db.init_db()
        # event_type 仅来自测试中的固定值。
        condition = f"WHEN NEW.event_type = '{event_type}'" if event_type else ""
        with db._connection() as connection:
            connection.execute(f"""CREATE TRIGGER reject_staff_events BEFORE INSERT ON staff_events
                {condition} BEGIN SELECT RAISE(ABORT, 'test event failure'); END""")

    def open_app(self):
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
        self.assertFalse(app.exception)
        return app

    def test_create_persists_normalized_json_and_returns_stable_id(self):
        result = self.add_staff(name="  测试护理员（虚构）  ", skills=[" 老人护理 ", "助浴", "老人护理"])
        self.assertIs(type(result["id"]), int)
        self.assertEqual(result["name"], "测试护理员（虚构）")
        self.assertEqual(result["skills"], ["老人护理", "助浴"])
        self.assertEqual(result["status"], "available")
        self.assertFalse(result["is_demo"])
        self.assertIsNotNone(datetime.fromisoformat(result["created_at"]).tzinfo)
        with db._connection() as connection:
            raw = connection.execute("SELECT * FROM staff WHERE id = ?", (result["id"],)).fetchone()
        self.assertIsInstance(raw["skills"], str)
        self.assertEqual(json.loads(raw["skills"]), result["skills"])
        self.assertEqual(staff.get_staff(result["id"]), result)

    def test_minimal_creation_allows_missing_optional_data(self):
        result = staff.create_staff("测试人员（虚构）")
        self.assertEqual((result["phone"], result["skills"], result["service_area"]), ("", [], ""))

    def test_invalid_creation_leaves_no_person_or_event(self):
        cases = ({"name": " "}, {"name": None}, {"skills": "护理"}, {"skills": [1]},
                 {"status": "assigned"}, {"is_demo": "true"}, {"phone": 123},
                 {"service_area": []}, {"actor_type": "demo"})
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.add_staff(**values)
        self.assertEqual(staff.list_staff(), [])
        self.assertEqual(self.event_count(), 0)

    def test_skill_parser_handles_json_lists_empty_values_and_deduplication(self):
        for value in ('["老人护理", " 助浴 ", "老人护理"]', ["老人护理", " 助浴 ", "老人护理"]):
            self.assertEqual(staff.parse_skills(value), ["老人护理", "助浴"])
        for value in (None, "", "[]", []):
            self.assertEqual(staff.parse_skills(value), [])

    def test_skill_parser_rejects_malformed_or_non_string_arrays(self):
        for value in ('{"skill": "护理"}', '"护理"', "null", "broken", [" "], [None], [True], 10):
            with self.subTest(value=value), self.assertRaises(ValueError):
                staff.parse_skills(value)

    def test_listing_empty_and_by_status(self):
        self.assertEqual(staff.list_staff(), [])
        people = [self.add_staff(status=status) for status in ("available", "busy", "inactive")]
        self.assertEqual(staff.list_staff(), people)
        for person in people:
            self.assertEqual(staff.list_staff(person["status"]), [person])

    def test_available_query_excludes_busy_and_inactive_and_can_exclude_demo(self):
        available = self.add_staff()
        demo = self.add_staff(is_demo=True)
        self.add_staff(status="busy")
        self.add_staff(status="inactive")
        self.assertEqual(staff.get_available_staff(), [available, demo])
        self.assertEqual(staff.get_available_staff(include_demo=False), [available])
        self.assertTrue(all(not row["is_demo"] for row in staff.list_staff(include_demo=False)))

    def test_invalid_query_filters_and_identifiers_are_rejected(self):
        self.add_staff()
        with self.assertRaises(ValueError):
            staff.list_staff("' OR 1=1 --")
        with self.assertRaises(ValueError):
            staff.list_staff(include_demo="false")
        for staff_id in (True, 0, -1, "1", "' OR 1=1 --"):
            with self.subTest(staff_id=staff_id), self.assertRaises(ValueError):
                staff.get_staff(staff_id)
        self.assertIsNone(staff.get_staff(999))
        self.assertEqual(events.get_staff_events(999), [])

    def test_creation_records_person_fields_source_and_demo_marker(self):
        person = self.add_staff(is_demo=True, actor_type="system")
        history = events.get_staff_events(person["id"])
        self.assertEqual(len(history), 1)
        event = history[0]
        self.assertEqual(event["event_type"], "staff_created")
        self.assertEqual(event["actor_type"], "system")
        self.assertEqual(event["before_data"], {})
        self.assertEqual(event["after_data"], {key: value for key, value in person.items() if key != "id"})
        self.assertIn("demo", event["reason"])
        self.assertTrue(event["operation_id"])

    def test_profile_update_records_only_changes_and_preserves_status_and_demo_marker(self):
        person = self.add_staff(is_demo=True)
        updated = staff.update_staff(person["id"], skills='["康复辅助"]', service_area="演示新区", reason="更新服务资料")
        self.assertEqual(updated["skills"], ["康复辅助"])
        self.assertEqual(updated["status"], "available")
        self.assertTrue(updated["is_demo"])
        event = events.get_staff_events(person["id"])[-1]
        self.assertEqual(event["event_type"], "staff_updated")
        self.assertEqual(event["before_data"], {"skills": ["老人护理", "助浴"], "service_area": "演示城区"})
        self.assertEqual(event["after_data"], {"skills": ["康复辅助"], "service_area": "演示新区"})
        self.assertEqual(event["reason"], "更新服务资料")
        self.assertEqual(staff.get_staff(person["id"]), updated)

    def test_profile_update_can_clear_optional_fields(self):
        person = self.add_staff(phone="测试联系方式")
        updated = staff.update_staff(person["id"], phone="", skills=[], service_area="")
        self.assertEqual((updated["phone"], updated["skills"], updated["service_area"]), ("", [], ""))

    def test_status_update_records_before_after_and_actor(self):
        person = self.add_staff()
        updated = staff.update_staff_status(person["id"], "busy", reason="人工标记忙碌")
        self.assertEqual(updated["status"], "busy")
        event = events.get_staff_events(person["id"])[-1]
        self.assertEqual(event["event_type"], "staff_status_changed")
        self.assertEqual(event["actor_type"], "operator")
        self.assertEqual(event["before_data"], {"status": "available"})
        self.assertEqual(event["after_data"], {"status": "busy"})
        self.assertEqual(event["reason"], "人工标记忙碌")
        self.assertEqual(staff.get_available_staff(), [])

    def test_unchanged_and_nonexistent_updates_do_not_create_events(self):
        person = self.add_staff()
        self.assertEqual(staff.update_staff(person["id"], skills=person["skills"]), person)
        self.assertEqual(staff.update_staff_status(person["id"], " available "), person)
        self.assertIsNone(staff.update_staff(999, name="不存在"))
        self.assertIsNone(staff.update_staff_status(999, "busy"))
        self.assertEqual(self.event_count(), 1)

    def test_invalid_updates_preserve_person_and_history(self):
        person = self.add_staff()
        for kwargs in ({"name": ""}, {"skills": [1]}, {"actor_type": "unknown"}, {"reason": None}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                staff.update_staff(person["id"], **kwargs)
        with self.assertRaises(ValueError):
            staff.update_staff_status(person["id"], "unknown")
        self.assertEqual(staff.get_staff(person["id"]), person)
        self.assertEqual(self.event_count(), 1)

    def test_event_history_filters_people_and_orders_changes(self):
        first, second = self.add_staff(), self.add_staff()
        staff.update_staff_status(first["id"], "busy")
        staff.update_staff(first["id"], skills=["康复辅助"])
        history = events.get_staff_events(first["id"])
        self.assertEqual([item["event_type"] for item in history], ["staff_created", "staff_status_changed", "staff_updated"])
        self.assertEqual([item["id"] for item in history], sorted(item["id"] for item in history))
        self.assertEqual(len({item["operation_id"] for item in history}), 3)
        self.assertEqual(len(events.get_staff_events(second["id"])), 1)

    def test_creation_rolls_back_when_event_fails(self):
        self.reject_events()
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_staff()
        self.assertEqual(staff.list_staff(), [])
        self.assertEqual(self.event_count(), 0)

    def test_profile_update_rolls_back_when_event_fails(self):
        person = self.add_staff()
        self.reject_events("staff_updated")
        with self.assertRaises(sqlite3.IntegrityError):
            staff.update_staff(person["id"], name="修改姓名（虚构）", skills=[])
        self.assertEqual(staff.get_staff(person["id"]), person)
        self.assertEqual(self.event_count(), 1)

    def test_status_update_rolls_back_when_event_fails(self):
        person = self.add_staff()
        self.reject_events("staff_status_changed")
        with self.assertRaises(sqlite3.IntegrityError):
            staff.update_staff_status(person["id"], "busy")
        self.assertEqual(staff.get_staff(person["id"]), person)
        self.assertEqual(self.event_count(), 1)

    def test_staff_event_requires_transaction_and_real_person(self):
        db.init_db()
        with db._connection() as connection:
            with self.assertRaises(ValueError):
                events.create_staff_event(connection, 999, "staff_created", "system", {}, {"name": "虚构"})
            connection.execute("BEGIN IMMEDIATE")
            with self.assertRaises(sqlite3.IntegrityError):
                events.create_staff_event(connection, 999, "staff_created", "system", {}, {"name": "虚构"})
        self.assertEqual(self.event_count(), 0)

    def test_demo_seed_is_explicit_marked_and_repeat_safe(self):
        db.init_db()
        self.assertEqual(staff.list_staff(), [])
        self.assertEqual(seed_demo_staff(), 3)
        people = staff.list_staff()
        self.assertEqual({person["status"] for person in people}, {"available", "busy", "inactive"})
        for person in people:
            self.assertTrue(person["is_demo"])
            self.assertIn("演示", person["name"])
            self.assertEqual(person["phone"], "")
            self.assertEqual(events.get_staff_events(person["id"])[0]["actor_type"], "system")
        self.assertEqual(seed_demo_staff(), 0)
        self.assertEqual(staff.list_staff(), people)
        self.assertEqual(self.event_count(), 3)
        self.assertEqual(staff.get_available_staff(include_demo=False), [])

    def test_demo_seed_does_not_mix_into_existing_resource_pool(self):
        person = self.add_staff()
        self.assertEqual(seed_demo_staff(), 0)
        self.assertEqual(staff.list_staff(), [person])

    def test_concurrent_demo_loading_inserts_one_batch(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            counts = list(pool.map(lambda _: seed_demo_staff(), range(2)))
        self.assertEqual(sorted(counts), [0, 3])
        self.assertEqual(len(staff.list_staff()), 3)
        self.assertEqual(self.event_count(), 3)

    def test_demo_batch_rolls_back_when_later_event_fails(self):
        db.init_db()
        with db._connection() as connection:
            connection.execute("""CREATE TRIGGER reject_second_staff BEFORE INSERT ON staff_events
                WHEN (SELECT COUNT(*) FROM staff_events) = 1
                BEGIN SELECT RAISE(ABORT, 'test second event failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            seed_demo_staff()
        self.assertEqual(staff.list_staff(), [])
        self.assertEqual(self.event_count(), 0)

    def test_legacy_ticket_database_migrates_without_fake_staff_or_events(self):
        with db._connection() as connection:
            connection.execute("""CREATE TABLE tickets (
                id TEXT PRIMARY KEY, customer_input TEXT, service_type TEXT, appointment_time TEXT,
                address TEXT, customer_name TEXT, phone TEXT, urgency TEXT, status TEXT, created_time TEXT)""")
            original = ("legacy", "旧工单（虚构）", "护理", "明天", "示例区", "演示客户", "", "普通", "待确认", "2026-10-01T10:00:00+08:00")
            connection.execute("INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", original)
        db.init_db()
        db.init_db()
        with db._connection() as connection:
            self.assertEqual(tuple(connection.execute("SELECT * FROM tickets").fetchone())[:10], original)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ticket_events").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM staff_events").fetchone()[0], 0)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(staff)")}
        self.assertEqual(columns, {"id", "name", "phone", "skills", "service_area", "status", "created_at", "is_demo"})
        self.assertEqual(staff.list_staff(), [])

    def test_existing_staff_schema_gets_demo_marker_without_rewriting_history(self):
        with db._connection() as connection:
            connection.execute("""CREATE TABLE staff (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                phone TEXT, skills TEXT, service_area TEXT, status TEXT, created_at TEXT)""")
            connection.execute(
                "INSERT INTO staff VALUES (?, ?, ?, ?, ?, ?, ?)",
                (7, "测试旧资料", "", json.dumps(["护理"], ensure_ascii=False), "示例区", "busy", "2026-10-01"),
            )
        db.init_db()
        person = staff.get_staff(7)
        self.assertEqual(person["skills"], ["护理"])
        self.assertEqual(person["created_at"], "2026-10-01")
        self.assertFalse(person["is_demo"])
        self.assertEqual(events.get_staff_events(7), [])
        db.init_db()
        self.assertEqual(staff.get_staff(7), person)

    def test_personnel_operations_preserve_ticket_data_events_and_ai_logs(self):
        db.save_ticket({"id": "existing", "customer_input": "虚构工单", "status": "待确认", "created_time": "2026-10-01T10:00:00+08:00"})
        ticket = db.get_ticket("existing")
        history = events.get_ticket_events("existing")
        ai_logging.LOG_PATH.write_text("[]", encoding="utf-8")
        person = self.add_staff()
        staff.update_staff(person["id"], service_area="示例新区")
        staff.update_staff_status(person["id"], "busy")
        self.assertEqual(db.get_ticket("existing"), ticket)
        self.assertEqual(events.get_ticket_events("existing"), history)
        self.assertEqual(ai_logging.LOG_PATH.read_text(encoding="utf-8"), "[]")
        self.parser_sdk.assert_not_called()
        self.client_sdk.assert_not_called()

    def test_dashboard_counts_states_and_identifies_demo_data(self):
        self.assertEqual(staff.get_staff_metrics(), {"staff_total_count": 0, "staff_available_count": 0, "staff_busy_count": 0, "staff_demo_count": 0})
        seed_demo_staff()
        person = self.add_staff()
        stats = dashboard.get_dashboard_stats()
        self.assertEqual({key: stats[key] for key in staff.get_staff_metrics()}, {
            "staff_total_count": 4, "staff_available_count": 2, "staff_busy_count": 1, "staff_demo_count": 3,
        })
        staff.update_staff_status(person["id"], "busy")
        self.assertEqual(staff.get_staff_metrics()["staff_available_count"], 1)
        self.assertEqual(staff.get_staff_metrics()["staff_busy_count"], 2)
        self.assertEqual(stats["total_tickets"], 0)
        self.assertEqual(stats["ai_call_count"], 0)

    def test_page_starts_empty_and_does_not_seed_on_refresh(self):
        app = self.open_app()
        app.run(timeout=30)
        self.assertFalse(app.exception)
        self.assertEqual(staff.list_staff(), [])
        self.assertEqual(self.event_count(), 0)
        self.assertTrue(any(item.label == "加载演示人员" for item in app.button))
        self.assertEqual(next(item.value for item in app.metric if item.label == "服务人员总数"), "0")
        self.parser_sdk.assert_not_called()
        self.client_sdk.assert_not_called()

    def test_page_loads_marked_demo_list_and_filters_without_new_events(self):
        app = self.open_app()
        next(item for item in app.button if item.label == "加载演示人员").click().run(timeout=30)
        self.assertFalse(app.exception)
        table = next(item.value for item in app.dataframe if "人员编号" in item.value.columns)
        self.assertEqual(len(table), 3)
        self.assertTrue(all(value == "演示数据（demo）" for value in table["数据来源"]))
        next(item for item in app.selectbox if item.label == "人员状态筛选").set_value("available").run(timeout=30)
        self.assertFalse(app.exception)
        table = next(item.value for item in app.dataframe if "人员编号" in item.value.columns)
        self.assertEqual(len(table), 1)
        self.assertEqual(table.iloc[0]["状态"], "可用")
        self.assertEqual(self.event_count(), 3)
        self.assertEqual(len(staff.list_staff()), 3)
        self.assertEqual(db.get_all_tickets(), [])
        metrics = {item.label: item.value for item in app.metric}
        self.assertEqual([metrics[label] for label in ("服务人员总数", "可用人员数量", "忙碌人员数量")], ["3", "1", "1"])
        self.parser_sdk.assert_not_called()
        self.client_sdk.assert_not_called()

    def test_page_shows_empty_status_filter_and_load_failure(self):
        person = self.add_staff()
        app = self.open_app()
        next(item for item in app.selectbox if item.label == "人员状态筛选").set_value("busy").run(timeout=30)
        self.assertFalse(app.exception)
        self.assertTrue(any("当前状态下暂无人员" in item.value for item in app.info))
        self.assertEqual(staff.get_staff(person["id"])["status"], "available")
        # 独立空库下验证加载失败显示提示且不残留人员或事件。
        with patch.object(db, "DB_PATH", self.directory / "failed-demo.db"):
            self.reject_events()
            app = self.open_app()
            next(item for item in app.button if item.label == "加载演示人员").click().run(timeout=30)
            self.assertFalse(app.exception)
            self.assertTrue(any("演示人员加载失败" in item.value for item in app.error))
            self.assertEqual(staff.list_staff(), [])
            self.assertEqual(self.event_count(), 0)


if __name__ == "__main__":
    unittest.main()
