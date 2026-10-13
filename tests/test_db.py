"""SQLite 持久化检查；每个测试使用临时数据库，不接触实际工单。"""

import json
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from database import db
from modules.tickets import create_ticket


EMPTY_PRIORITY = dict.fromkeys(("priority_suggested", "priority_final", "priority_reason", "priority_source"))
EMPTY_ADVICE = dict.fromkeys(("advice_json", "advice_source", "advice_decision", "advice_final", "advice_generated_at"))
NEW_ADVICE = {**EMPTY_ADVICE, "advice_decision": "unreviewed"}


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db_path = Path(directory.name) / "data" / "autoflow.db"
        patcher = patch.object(db, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fields = {
            "service_type": "空调清洗",
            "appointment_time": "周六上午",
            "address": "示例路1号",
            "customer_name": "演示客户",
            "phone": "演示电话001",
            "urgency": "普通",
        }
        self.base_ticket = {
            "id": "test-ticket",
            "customer_input": "请清洗空调。",
            **self.fields,
            "status": "待确认",
            "created_time": "2026-10-11T10:00:00+08:00",
        }
        self.ai_result = json.dumps(
            {field: {"value": value, "confidence": 0.75} for field, value in self.fields.items()},
            ensure_ascii=False,
        )

    def test_initialization_creates_expected_table_and_empty_history(self):
        db.init_db()
        self.assertTrue(self.db_path.is_file())
        self.assertEqual(db.get_all_tickets(), [])
        with closing(sqlite3.connect(self.db_path)) as connection:
            columns = connection.execute("PRAGMA table_info(tickets)").fetchall()
        self.assertEqual(
            [column[1] for column in columns],
            ["id", "customer_input", "service_type", "appointment_time", "address",
             "customer_name", "phone", "urgency", "status", "created_time",
             "confidence_score", "review_required", "token_used", "response_time",
             "priority_suggested", "priority_final", "priority_reason", "priority_source", *EMPTY_ADVICE],
        )
        self.assertEqual([column[2] for column in columns], ["TEXT"] * 12 + ["INTEGER", "REAL"] + ["TEXT"] * 9)
        self.assertEqual(columns[0][5], 1)
        self.assertEqual(columns[10][3:5], (1, "'{}'"))
        self.assertEqual(columns[11][3:5], (1, "'false'"))
        self.assertEqual(columns[12][3:5], (1, "0"))
        self.assertEqual(columns[13][3:5], (0, "NULL"))

    def test_old_table_migration_preserves_data_and_accepts_new_tickets(self):
        self.db_path.parent.mkdir(parents=True)
        legacy_ticket = {**self.base_ticket, "status": "已完成"}
        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE tickets (
                        id TEXT PRIMARY KEY NOT NULL,
                        customer_input TEXT NOT NULL,
                        service_type TEXT NOT NULL DEFAULT '',
                        appointment_time TEXT NOT NULL DEFAULT '',
                        address TEXT NOT NULL DEFAULT '',
                        customer_name TEXT NOT NULL DEFAULT '',
                        phone TEXT NOT NULL DEFAULT '',
                        urgency TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL,
                        created_time TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """INSERT INTO tickets (
                        id, customer_input, service_type, appointment_time, address,
                        customer_name, phone, urgency, status, created_time
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    tuple(legacy_ticket.values()),
                )

        db.init_db()
        db.init_db()
        expected_legacy = {
            **legacy_ticket, "confidence_score": "{}", "review_required": False,
            "token_used": 0, "response_time": None,
            **EMPTY_PRIORITY, **EMPTY_ADVICE,
        }
        self.assertEqual(db.get_ticket(legacy_ticket["id"]), expected_legacy)
        new_ticket = {
            **self.base_ticket, "id": "new-ticket", "status": "人工审核",
            "confidence_score": self.ai_result, "review_required": True,
            "token_used": 37, "response_time": 0.12,
            **EMPTY_PRIORITY, **NEW_ADVICE,
        }
        db.save_ticket(new_ticket)
        db.init_db()
        self.assertEqual(db.get_ticket(new_ticket["id"]), new_ticket)
        self.assertEqual(db.get_ticket(legacy_ticket["id"]), expected_legacy)
        with closing(sqlite3.connect(self.db_path)) as connection:
            columns = connection.execute("PRAGMA table_info(tickets)").fetchall()
        self.assertEqual(len(columns), 23)

    def test_twelve_column_migration_preserves_review_state_and_original_ai_json(self):
        self.db_path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE tickets (
                        id TEXT PRIMARY KEY NOT NULL,
                        customer_input TEXT NOT NULL,
                        service_type TEXT NOT NULL DEFAULT '',
                        appointment_time TEXT NOT NULL DEFAULT '',
                        address TEXT NOT NULL DEFAULT '',
                        customer_name TEXT NOT NULL DEFAULT '',
                        phone TEXT NOT NULL DEFAULT '',
                        urgency TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL,
                        created_time TEXT NOT NULL,
                        confidence_score TEXT NOT NULL DEFAULT '{}',
                        review_required TEXT NOT NULL DEFAULT 'false'
                    )
                    """
                )
                for ticket_id, status, review in (
                    ("needs-review", "人工审核", "true"),
                    ("completed", "已完成", "false"),
                ):
                    ticket = {**self.base_ticket, "id": ticket_id, "status": status}
                    connection.execute(
                        """INSERT INTO tickets (
                            id, customer_input, service_type, appointment_time, address,
                            customer_name, phone, urgency, status, created_time,
                            confidence_score, review_required
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (*ticket.values(), self.ai_result, review),
                    )

        db.init_db()
        db.init_db()
        for ticket_id, status, review in (
            ("needs-review", "人工审核", True),
            ("completed", "已完成", False),
        ):
            with self.subTest(ticket_id=ticket_id):
                expected = {
                    **self.base_ticket, "id": ticket_id, "status": status,
                    "confidence_score": self.ai_result, "review_required": review,
                    "token_used": 0, "response_time": None,
                    **EMPTY_PRIORITY, **EMPTY_ADVICE,
                }
                self.assertEqual(db.get_ticket(ticket_id), expected)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(len(connection.execute("PRAGMA table_info(tickets)").fetchall()), 23)
            self.assertIsNone(connection.execute("SELECT AVG(response_time) FROM tickets").fetchone()[0])

        measured_ticket = {
            **self.base_ticket, "id": "new-measurement", "confidence_score": "{}",
            "review_required": False, "token_used": 120, "response_time": 1.25,
            **EMPTY_PRIORITY, **NEW_ADVICE,
        }
        db.save_ticket(measured_ticket)
        db.init_db()
        self.assertEqual(db.get_ticket(measured_ticket["id"]), measured_ticket)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT AVG(response_time) FROM tickets").fetchone()[0], 1.25)

    def test_measured_metrics_round_trip_and_status_updates_preserve_them(self):
        for token_used, response_time in ((0, None), (321, 1.75), (0, 0.0)):
            with self.subTest(token_used=token_used, response_time=response_time):
                ticket = {
                    **self.base_ticket, "id": f"measurement-{response_time}",
                    "confidence_score": self.ai_result, "review_required": False,
                    "token_used": token_used, "response_time": response_time,
                    **EMPTY_PRIORITY, **NEW_ADVICE,
                }
                db.save_ticket(ticket)
                self.assertEqual(db.get_ticket(ticket["id"]), ticket)
                self.assertIn(ticket, db.get_all_tickets())
                self.assertTrue(db.update_ticket_status(ticket["id"], "已完成"))
                self.assertEqual(db.get_ticket(ticket["id"]), {**ticket, "status": "已完成"})

    def test_invalid_metrics_are_rejected_without_saving_a_ticket(self):
        invalid_metrics = [
            {"token_used": value} for value in (-1, True, 1.5, "2", None)
        ] + [
            {"response_time": value} for value in (-0.5, True, "1.5", float("nan"), float("inf"))
        ]
        for metrics in invalid_metrics:
            with self.subTest(metrics=metrics):
                with self.assertRaises(ValueError):
                    db.save_ticket({**self.base_ticket, **metrics})
        self.assertEqual(db.get_all_tickets(), [])

    def test_saving_legacy_fixture_uses_empty_ai_data_and_false_review_flag(self):
        db.save_ticket(self.base_ticket)
        expected = {
            **self.base_ticket, "confidence_score": "{}", "review_required": False,
            "token_used": 0, "response_time": None,
            **EMPTY_PRIORITY, **NEW_ADVICE,
        }
        self.assertEqual(db.get_all_tickets(), [expected])
        self.assertEqual(db.get_ticket(self.base_ticket["id"]), expected)
        self.assertIsNone(db.get_ticket("unknown-id"))

    def test_review_flags_round_trip_as_booleans_and_ai_json_is_unchanged(self):
        for review_required in (False, True):
            with self.subTest(review_required=review_required):
                ticket = {
                    **self.base_ticket, "id": f"review-{review_required}",
                    "confidence_score": self.ai_result, "review_required": review_required,
                    "token_used": 0, "response_time": None,
                    **EMPTY_PRIORITY, **NEW_ADVICE,
                }
                db.save_ticket(ticket)
                saved = db.get_ticket(ticket["id"])
                self.assertEqual(saved, ticket)
                self.assertIs(saved["review_required"], review_required)
                with closing(sqlite3.connect(self.db_path)) as connection:
                    row = connection.execute(
                        "SELECT confidence_score, review_required FROM tickets WHERE id = ?",
                        (ticket["id"],),
                    ).fetchone()
                self.assertEqual(row, (self.ai_result, "true" if review_required else "false"))
        self.assertTrue(all(type(row["review_required"]) is bool for row in db.get_all_tickets()))

    def test_status_updates_cannot_skip_review_and_can_request_review(self):
        ticket = {
            **self.base_ticket, "status": "人工审核",
            "confidence_score": self.ai_result, "review_required": True,
            "token_used": 0, "response_time": None,
            **EMPTY_PRIORITY, **NEW_ADVICE,
        }
        db.save_ticket(ticket)
        with self.assertRaisesRegex(ValueError, "该工单需要先完成人工审核。"):
            db.update_ticket_status(ticket["id"], "已完成")
        self.assertEqual(db.get_ticket(ticket["id"]), ticket)

        ordinary = {**self.base_ticket, "id": "ordinary"}
        db.save_ticket(ordinary)
        self.assertTrue(db.update_ticket_status(ordinary["id"], " 人工审核 "))
        saved = db.get_ticket(ordinary["id"])
        self.assertEqual(saved["status"], "人工审核")
        self.assertIs(saved["review_required"], True)
        with self.assertRaisesRegex(ValueError, "该工单需要先完成人工审核。"):
            db.update_ticket_status(ordinary["id"], "待确认")
        self.assertEqual(db.get_ticket(ordinary["id"]), saved)

    def test_review_saves_corrected_fields_and_preserves_original_ai_data(self):
        ticket = {
            **self.base_ticket, "status": "人工审核",
            "confidence_score": self.ai_result, "review_required": True,
            "token_used": 250, "response_time": 1.125,
            **EMPTY_PRIORITY, **NEW_ADVICE,
        }
        db.save_ticket(ticket)
        corrections = {
            "service_type": "保洁", "appointment_time": "周日中午",
            "address": "修正路2号", "customer_name": "O'Reilly",
            "phone": "演示电话100", "urgency": "紧急",
        }
        self.assertTrue(db.update_ticket_after_review(ticket["id"], corrections, "待确认"))
        expected = {**ticket, **corrections, "status": "待确认", "review_required": False}
        self.assertEqual(db.get_ticket(ticket["id"]), expected)
        self.assertEqual(db.get_all_tickets(), [expected])
        self.assertFalse(db.update_ticket_after_review(ticket["id"], self.fields, "已完成"))
        self.assertFalse(db.update_ticket_after_review("unknown-id", self.fields, "已完成"))
        self.assertEqual(db.get_ticket(ticket["id"]), expected)

    def test_creation_saves_final_status_and_all_fields(self):
        raw = "请清洗空调，地址示例路1号，电话演示电话001。"
        ticket = create_ticket(raw, self.fields)
        self.assertEqual(ticket["status"], "待确认")
        self.assertEqual(db.get_all_tickets(), [ticket])
        self.assertEqual(ticket["customer_input"], raw)
        self.assertEqual(
            datetime.fromisoformat(ticket["created_time"]).utcoffset(), timedelta(hours=8)
        )

    def test_missing_contact_information_is_saved_as_pending_information(self):
        for missing in ("phone", "address"):
            with self.subTest(missing=missing):
                fields = {**self.fields, missing: ""}
                ticket = create_ticket("测试需求", fields)
                self.assertEqual(ticket["status"], "待补充信息")
        self.assertEqual(len(db.get_all_tickets()), 2)

    def test_reinitializing_and_reopening_preserve_ticket(self):
        ticket = create_ticket("重启后仍保留的需求", self.fields)
        db.init_db()
        db.init_db()
        # 独立 SQLite 连接读到已提交的数据，模拟重新打开数据库。
        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                "SELECT customer_input, status FROM tickets WHERE id = ?", (ticket["id"],)
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row, (ticket["customer_input"], "待确认"))
        self.assertEqual(db.get_all_tickets(), [ticket])

    def test_customer_text_with_quotes_is_data_and_does_not_change_schema(self):
        text = "客户说：O'Reilly'); DROP TABLE tickets; --"
        ticket = create_ticket(text, self.fields)
        self.assertEqual(db.get_all_tickets(), [ticket])
        create_ticket("另一条工单", self.fields)
        self.assertEqual(len(db.get_all_tickets()), 2)

    def test_duplicate_id_fails_without_overwriting_existing_ticket(self):
        ticket = create_ticket("原始需求", self.fields)
        with self.assertRaises(sqlite3.IntegrityError):
            db.save_ticket({**ticket, "customer_input": "不应覆盖原记录"})
        self.assertEqual(db.get_all_tickets(), [ticket])

    def test_history_is_returned_newest_first(self):
        ticket = create_ticket("较新的工单", self.fields)
        older = {**ticket, "id": "older-ticket", "created_time": "2020-01-01T00:00:00+08:00"}
        db.save_ticket(older)
        self.assertEqual([row["id"] for row in db.get_all_tickets()], [ticket["id"], older["id"]])

    def test_status_update_changes_only_selected_ticket(self):
        first = create_ticket("第一条", self.fields)
        second = create_ticket("第二条", self.fields)
        self.assertTrue(db.update_ticket_status(first["id"], " 已完成 "))
        rows = {row["id"]: row for row in db.get_all_tickets()}
        self.assertEqual(rows[first["id"]], {**first, "status": "已完成"})
        self.assertEqual(rows[second["id"]], second)

    def test_unknown_id_and_blank_status_do_not_change_existing_data(self):
        ticket = create_ticket("测试需求", self.fields)
        self.assertFalse(db.update_ticket_status("unknown-id", "已完成"))
        with self.assertRaises(ValueError):
            db.update_ticket_status(ticket["id"], " \t ")
        self.assertEqual(db.get_all_tickets(), [ticket])

    def test_database_write_failure_propagates_instead_of_returning_ticket(self):
        with patch("modules.tickets.save_ticket", side_effect=sqlite3.OperationalError("test")):
            with self.assertRaises(sqlite3.OperationalError):
                create_ticket("不能保存的需求", self.fields)
        self.assertEqual(db.get_all_tickets(), [])


if __name__ == "__main__":
    unittest.main()
