"""信息补全、事务、人工保护、独立 AI 日志和页面的离线回归测试。"""

import json
import os
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from openai import APITimeoutError
from streamlit.testing.v1 import AppTest

import config
from database import db
from modules import ai_client, ai_logging, ai_parser, conversation, dashboard, events, priority
from modules.rules import COMPLETION_FIELDS
from modules.tickets import confirm_advice, confirm_priority, create_ticket, generate_ticket_advice, generate_ticket_priority, review_ticket


class ConversationTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self._patch(patch.object(db, "DB_PATH", self.directory / "tickets.db"))
        self._patch(patch.object(ai_logging, "LOG_PATH", self.directory / "ai_calls.json"))
        self._patch(patch.object(config, "DEMO_MODE", True))
        self._patch(patch.object(ai_parser, "load_dotenv"))
        self._patch(patch.dict(os.environ, {"OPENAI_API_KEY": "offline-conversation-test-key"}))
        self.sdk = self._patch(patch.object(ai_client, "OpenAI"))
        self.client = self.sdk.return_value.__enter__.return_value
        self.parser = self._patch(patch.object(ai_parser, "parse_customer_request"))
        self.fields = {"customer_name": "演示客户", "phone": "演示电话001", "address": "示例路1号",
                       "service_type": "居家保洁", "appointment_time": "明天下午两点", "urgency": "普通"}

    def _patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def create(self, **fields):
        return create_ticket("虚构需求", {**self.fields, **fields})

    def start(self, **fields):
        ticket = self.create(**fields)
        return conversation.start_conversation(ticket["id"])

    def state(self, ticket):
        return db.get_conversation_state(ticket["id"])

    def history(self, ticket, kind):
        return [event for event in events.get_ticket_events(ticket["id"]) if event["event_type"] == kind]

    def enable_ai(self, **fields):
        config.DEMO_MODE = False
        self.client.responses.create.return_value = SimpleNamespace(
            status="completed", output_text=json.dumps({**dict.fromkeys(COMPLETION_FIELDS, ""), **fields}, ensure_ascii=False),
            usage=SimpleNamespace(total_tokens=61), model="conversation-test-model",
        )

    def block_event(self, kind):
        self.assertIn(kind, ("conversation_started", "conversation_question_generated", "field_filled", "conversation_completed"))
        with db._connection() as connection:
            connection.execute(f"""CREATE TRIGGER fail_conversation_event BEFORE INSERT ON ticket_events
                WHEN NEW.event_type = '{kind}' BEGIN SELECT RAISE(ABORT, 'test rollback'); END""")

    def app(self):
        result = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
        self.assertFalse(result.exception)
        return result

    def click(self, app, label):
        next(item for item in app.button if item.label == label).click().run(timeout=30)
        self.assertFalse(app.exception)

    def test_detect_missing_fields_includes_all_five_and_ignores_urgency(self):
        for field in COMPLETION_FIELDS:
            for value in (None, "", " "):
                self.assertEqual(conversation.detect_missing_fields({**self.fields, field: value}), {"missing": [field]})
        self.assertEqual(conversation.detect_missing_fields({**self.fields, "urgency": ""}), {"missing": []})
        self.assertEqual(conversation.detect_missing_fields({}), {"missing": list(COMPLETION_FIELDS)})

    def test_questions_are_stable_only_for_missing_fields_without_api(self):
        ticket = {**self.fields, "address": "", "phone": ""}
        result = conversation.generate_questions(ticket, {"missing": ["address", "phone", "customer_name", "bogus"]})
        self.assertEqual(len(result), 2)
        self.assertIn("联系电话", result[0])
        self.assertIn("服务地址", result[1])
        self.sdk.assert_not_called()

    def test_creation_and_read_only_queries_do_not_start_conversations(self):
        ticket = self.create(phone="")
        self.assertEqual(self.state(ticket)["messages"], [])
        self.assertEqual(self.state(ticket)["missing_fields"], [])
        dashboard.get_dashboard_stats()
        self.assertEqual(self.history(ticket, "conversation_started"), [])
        self.sdk.assert_not_called()

    def test_start_records_system_question_missing_rows_and_events_once(self):
        ticket = self.start(customer_name="", phone="")
        first = self.state(ticket)
        self.assertEqual([message["role"] for message in first["messages"]], ["system", "assistant"])
        self.assertEqual(first["question_field"], "customer_name")
        self.assertEqual({row["field_name"] for row in first["missing_fields"]}, {"customer_name", "phone"})
        self.assertTrue(all(row["status"] == "missing" and row["filled_at"] is None for row in first["missing_fields"]))
        conversation.start_conversation(ticket["id"])
        self.assertEqual(self.state(ticket), first)
        self.assertEqual(len(self.history(ticket, "conversation_started")), 1)
        self.assertEqual(len(self.history(ticket, "conversation_question_generated")), 1)

    def test_multi_turn_answers_update_original_ticket_and_complete(self):
        ticket = self.start(customer_name="", phone="", address="")
        filled_name = conversation.process_reply(ticket["id"], "我叫演示甲")
        self.assertEqual(filled_name["customer_name"], "演示甲")
        self.assertEqual(self.state(ticket)["question_field"], "phone")
        conversation.process_reply(ticket["id"], "演示电话002")
        self.assertEqual(self.state(ticket)["question_field"], "address")
        complete = conversation.process_reply(ticket["id"], "示例小区2号")
        self.assertEqual(conversation.detect_missing_fields(complete)["missing"], [])
        self.assertEqual(complete["status"], "待确认")
        self.assertEqual([row["id"] for row in db.get_all_tickets()], [ticket["id"]])
        self.assertEqual(len(self.history(ticket, "field_filled")), 3)
        self.assertEqual(len(self.history(ticket, "conversation_completed")), 1)
        self.assertEqual(len(self.state(ticket)["messages"]), 8)
        self.assertTrue(all(row["status"] == "filled" and row["filled_at"] for row in self.state(ticket)["missing_fields"]))
        self.parser.assert_not_called()

    def test_one_reply_can_fill_several_labelled_fields(self):
        ticket = self.start(customer_name="", phone="", address="", service_type="", appointment_time="")
        saved = conversation.process_reply(ticket["id"], "姓名：演示甲；电话：演示电话002；地址：示例路2号；服务类型：居家保洁；预约时间：周六上午")
        self.assertEqual(saved["customer_name"], "演示甲")
        self.assertEqual(saved["phone"], "演示电话002")
        self.assertEqual(saved["appointment_time"], "周六上午")
        self.assertEqual(conversation.detect_missing_fields(saved), {"missing": []})

    def test_existing_values_and_confirmed_priority_advice_are_not_overwritten(self):
        ticket = self.start(phone="")
        confirm_priority(ticket["id"], "高", "运营确认")
        generate_ticket_advice(ticket["id"])
        before = confirm_advice(ticket["id"], "modified", "人工最终方案", "运营决定")
        saved = conversation.process_reply(ticket["id"], "姓名：其他演示人；地址：其他演示地址；电话：演示电话002")
        for field in set(before) - {"phone", "status"}:
            self.assertEqual(saved[field], before[field])
        event = self.history(ticket, "field_filled")[0]
        self.assertEqual(event["before_data"], {"phone": ""})
        self.assertEqual(event["after_data"]["phone"], "演示电话002")
        self.assertEqual(event["after_data"]["source"], "demo")

    def test_original_review_gate_and_ai_snapshot_remain_after_completion(self):
        parsed = {field: {"value": value, "confidence": 0.95} for field, value in self.fields.items()}
        parsed["phone"] = {"value": "", "confidence": 0}
        ticket = create_ticket("缺电话的虚构需求", parsed)
        conversation.start_conversation(ticket["id"])
        saved = conversation.process_reply(ticket["id"], "演示电话002")
        self.assertTrue(saved["review_required"])
        self.assertEqual(saved["status"], "人工审核")
        self.assertEqual(saved["confidence_score"], ticket["confidence_score"])
        with self.assertRaises(ValueError):
            db.update_ticket_status(ticket["id"], "待派单")

    def test_manual_clear_is_ignored_by_automatic_completion_but_still_incomplete(self):
        ticket = self.create()
        db.update_ticket_status(ticket["id"], "人工审核")
        review_ticket(ticket["id"], {**self.fields, "phone": ""})
        conversation.start_conversation(ticket["id"])
        state = self.state(ticket)
        self.assertIn("phone", state["protected_fields"])
        self.assertEqual(state["missing_fields"][0]["status"], "ignored")
        with self.assertRaisesRegex(ValueError, "人工清空"):
            conversation.process_reply(ticket["id"], "电话：演示电话002")
        self.assertEqual(db.get_ticket(ticket["id"])["phone"], "")
        self.assertEqual(self.history(ticket, "conversation_completed"), [])

    def test_manual_review_can_complete_active_conversation_in_same_transaction(self):
        ticket = self.start(phone="")
        db.update_ticket_status(ticket["id"], "人工审核")
        reviewed = review_ticket(ticket["id"], {**self.fields, "phone": "演示电话003"})
        self.assertEqual(self.state(ticket)["missing_fields"][0]["status"], "filled")
        self.assertEqual(self.history(ticket, "field_filled")[-1]["actor_type"], "operator")
        self.assertEqual(self.history(ticket, "conversation_completed")[-1]["actor_type"], "operator")
        self.assertEqual(reviewed["phone"], "演示电话003")

    def test_manual_review_updates_next_question_without_overwriting_fields(self):
        ticket = self.start(customer_name="", phone="")
        db.update_ticket_status(ticket["id"], "人工审核")
        review_ticket(ticket["id"], {**self.fields, "customer_name": "人工确认客户", "phone": ""})
        self.assertEqual(self.state(ticket)["question_field"], "phone")
        saved = conversation.process_reply(ticket["id"], "演示电话002")
        self.assertEqual(saved["customer_name"], "人工确认客户")

    def test_only_missing_appointment_remains_pending_confirmation(self):
        ticket = self.start(phone="", appointment_time="")
        saved = conversation.process_reply(ticket["id"], "电话：演示电话002")
        self.assertEqual(saved["status"], "待确认")
        self.assertEqual(conversation.detect_missing_fields(saved), {"missing": ["appointment_time"]})
        self.assertEqual(self.history(ticket, "conversation_completed"), [])

    def test_address_gate_prevents_dispatch_and_service_without_changing_ticket(self):
        ticket = self.create(address="")
        for status in ("待派单", " 服务中 "):
            with self.assertRaisesRegex(ValueError, "缺少服务地址"):
                db.update_ticket_status(ticket["id"], status)
            self.assertEqual(db.get_ticket(ticket["id"]), ticket)
        conversation.start_conversation(ticket["id"])
        conversation.process_reply(ticket["id"], "地址：示例路2号")
        self.assertTrue(db.update_ticket_status(ticket["id"], "待派单"))

    def test_no_phone_does_not_lower_suggested_or_confirmed_priority(self):
        ticket = self.start(phone="", urgency="紧急")
        self.assertEqual(ticket["priority_suggested"], "高")
        before = confirm_priority(ticket["id"], "高", "已确认")
        conversation.process_reply(ticket["id"], "稍后提供")
        for field in db.PRIORITY_FIELDS:
            self.assertEqual(db.get_ticket(ticket["id"])[field], before[field])

    def test_uncertain_or_invalid_replies_are_saved_without_fabricating_fields(self):
        ticket = self.start(phone="")
        for reply in ("不知道", "电话：123", "暂时不方便提供", "联系电话：未知", "谢谢"):
            saved = conversation.process_reply(ticket["id"], reply)
            self.assertEqual(saved["phone"], "")
            self.assertEqual(self.state(ticket)["messages"][-2]["content"], reply)
        self.assertEqual(self.history(ticket, "field_filled"), [])

    def test_conflicting_phone_numbers_and_duplicate_labels_require_clarification(self):
        ticket = self.start(phone="")
        for reply in ("电话：演示电话002；电话：演示电话003", "演示电话002或演示电话003"):
            self.assertEqual(conversation.process_reply(ticket["id"], reply)["phone"], "")

    def test_ai_success_has_independent_schema_logs_and_grounding(self):
        self.enable_ai(phone="演示电话002", address="虚构无依据地址")
        ticket = self.start(phone="", address="")
        saved = conversation.process_reply(ticket["id"], "请记一下演示电话002，这是联系电话")
        self.assertEqual(saved["phone"], "演示电话002")
        self.assertEqual(saved["address"], "")
        call = self.client.responses.create.call_args.kwargs
        self.assertTrue(call["text"]["format"]["strict"])
        self.assertEqual(set(call["text"]["format"]["schema"]["required"]), set(COMPLETION_FIELDS))
        self.assertFalse(call["store"])
        entry = ai_logging.read_ai_calls()[-1]
        self.assertEqual(entry["task_type"], "conversation")
        self.assertEqual(entry["status"], "success")
        self.assertEqual(entry["token_used"], 61)
        self.assertGreaterEqual(entry["response_time"], 0)
        self.parser.assert_not_called()

    def test_api_timeout_or_error_uses_rule_reply_parser(self):
        self.enable_ai()
        for error in (APITimeoutError(request=SimpleNamespace()), RuntimeError("private-error")):
            ticket = self.start(phone="", address="")
            self.client.responses.create.side_effect = error
            saved = conversation.process_reply(ticket["id"], "电话：演示电话002")
            self.assertEqual(saved["phone"], "演示电话002")
            self.assertEqual(self.history(ticket, "field_filled")[0]["after_data"]["source"], "rule")
            self.assertIn("AI不可用", self.state(ticket)["messages"][-1]["content"])
            self.assertNotIn("private-error", json.dumps(ai_logging.read_ai_calls()))

    def test_json_errors_and_invalid_ai_types_use_rules(self):
        self.enable_ai()
        for text in ("bad-json", "[]", "{}", json.dumps({**dict.fromkeys(COMPLETION_FIELDS, ""), "phone": 123})):
            ticket = self.start(phone="")
            self.client.responses.create.return_value.output_text = text
            saved = conversation.process_reply(ticket["id"], "电话：演示电话002")
            self.assertEqual(saved["phone"], "演示电话002")
            self.assertEqual(ai_logging.read_ai_calls()[-1]["status"], "invalid_response")

    def test_missing_key_and_sdk_wrapper_exception_both_use_rules(self):
        self.enable_ai()
        ticket = self.start(phone="")
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self.assertEqual(conversation.process_reply(ticket["id"], "演示电话002")["phone"], "演示电话002")
        self.sdk.assert_not_called()
        ticket = self.start(phone="")
        with patch.object(conversation, "request_conversation", side_effect=ValueError("configuration")):
            self.assertEqual(conversation.process_reply(ticket["id"], "演示电话002")["phone"], "演示电话002")

    def test_demo_ticket_stays_offline_when_configuration_switches(self):
        ticket = self.start(phone="")
        self.enable_ai()
        conversation.process_reply(ticket["id"], "演示电话002")
        self.sdk.assert_not_called()
        self.assertEqual(ai_logging.read_ai_calls(), [])

    def test_log_failure_does_not_retry_or_lose_valid_completion(self):
        self.enable_ai(phone="演示电话002")
        ticket = self.start(phone="")
        with patch.object(ai_client, "append_ai_call", side_effect=OSError("test log failure")):
            saved = conversation.process_reply(ticket["id"], "演示电话002")
        self.assertEqual(saved["phone"], "演示电话002")
        self.client.responses.create.assert_called_once()

    def test_four_task_types_are_isolated(self):
        for task in ("parse", "priority", "advisor"):
            ai_logging.append_ai_call({"task_type": task, "api_called": True, "status": "success", "response_time": 1.0})
        before = {task: ai_logging.get_ai_call_stats(task) for task in ("parse", "priority", "advisor")}
        self.enable_ai(phone="演示电话002")
        ticket = self.start(phone="")
        conversation.process_reply(ticket["id"], "演示电话002")
        for task in before:
            self.assertEqual(ai_logging.get_ai_call_stats(task), before[task])
        self.assertEqual(ai_logging.get_ai_call_stats("conversation")["ai_call_count"], 1)

    def test_stale_ticket_during_ai_request_is_rejected(self):
        self.enable_ai()
        ticket = self.start(phone="")
        original_state = self.state(ticket)
        def during_call(*args):
            confirm_priority(ticket["id"], "高", "运营先行确认")
            return {"phone": "演示电话002"}
        with patch.object(conversation, "request_conversation", side_effect=during_call):
            with self.assertRaisesRegex(ValueError, "已更新"):
                conversation.process_reply(ticket["id"], "演示电话002")
        self.assertEqual(db.get_ticket(ticket["id"])["phone"], "")
        self.assertEqual(self.state(ticket), original_state)

    def test_stale_message_version_is_rejected_before_api_call(self):
        self.enable_ai()
        ticket = self.start(phone="", address="")
        version = self.state(ticket)["last_message_id"]
        conversation.process_reply(ticket["id"], "电话：演示电话002")
        count = self.client.responses.create.call_count
        with self.assertRaisesRegex(ValueError, "对话已更新"):
            conversation.process_reply(ticket["id"], "地址：示例路2号", expected_message_id=version)
        self.assertEqual(self.client.responses.create.call_count, count)

    def test_start_event_or_question_failure_rolls_back_messages_and_tracking(self):
        for kind in ("conversation_started", "conversation_question_generated"):
            ticket = self.create(phone="")
            self.block_event(kind)
            with self.assertRaises(sqlite3.IntegrityError):
                conversation.start_conversation(ticket["id"])
            self.assertEqual(self.state(ticket)["messages"], [])
            self.assertEqual(self.state(ticket)["missing_fields"], [])
            self.assertEqual(self.history(ticket, "conversation_started"), [])
            with db._connection() as connection:
                connection.execute("DROP TRIGGER fail_conversation_event")

    def test_reply_field_or_completion_event_failure_rolls_back_everything(self):
        for kind in ("field_filled", "conversation_completed", "conversation_question_generated"):
            ticket = self.start(phone="", address="" if kind == "conversation_question_generated" else self.fields["address"])
            state, history = self.state(ticket), events.get_ticket_events(ticket["id"])
            self.block_event(kind)
            with self.assertRaises(sqlite3.IntegrityError):
                conversation.process_reply(ticket["id"], "演示电话002")
            self.assertEqual(db.get_ticket(ticket["id"]), ticket)
            self.assertEqual(self.state(ticket), state)
            self.assertEqual(events.get_ticket_events(ticket["id"]), history)
            with db._connection() as connection:
                connection.execute("DROP TRIGGER fail_conversation_event")

    def test_manual_review_rolls_back_if_completion_event_fails(self):
        ticket = self.start(phone="")
        db.update_ticket_status(ticket["id"], "人工审核")
        before, state = db.get_ticket(ticket["id"]), self.state(ticket)
        self.block_event("conversation_completed")
        with self.assertRaises(sqlite3.IntegrityError):
            review_ticket(ticket["id"], self.fields)
        self.assertEqual(db.get_ticket(ticket["id"]), before)
        self.assertEqual(self.state(ticket), state)

    def test_migration_adds_empty_tables_without_changing_old_tickets_or_events(self):
        ticket = self.create(phone="")
        history = events.get_ticket_events(ticket["id"])
        with db._connection() as connection:
            connection.execute("DROP TABLE conversation_messages")
            connection.execute("DROP TABLE ticket_missing_fields")
        db.init_db()
        db.init_db()
        self.assertEqual(db.get_ticket(ticket["id"]), ticket)
        self.assertEqual(events.get_ticket_events(ticket["id"]), history)
        self.assertEqual(self.state(ticket)["messages"], [])
        self.assertEqual(self.state(ticket)["missing_fields"], [])
        with db._connection() as connection:
            self.assertEqual([row["name"] for row in connection.execute("PRAGMA table_info(conversation_messages)")],
                             ["id", "ticket_id", "role", "content", "created_at"])
            self.assertEqual([row["name"] for row in connection.execute("PRAGMA table_info(ticket_missing_fields)")],
                             ["id", "ticket_id", "field_name", "status", "created_at", "filled_at"])

    def test_constraints_and_reinitialization_preserve_conversation_history(self):
        ticket = self.start(phone="")
        conversation.process_reply(ticket["id"], "演示电话002")
        state = self.state(ticket)
        db.init_db()
        self.assertEqual(self.state(ticket), state)
        with self.assertRaises(sqlite3.IntegrityError), db._connection() as connection:
            connection.execute("INSERT INTO conversation_messages (ticket_id, role, content, created_at) VALUES (?, 'invalid', '', '')", (ticket["id"],))
        with self.assertRaises(sqlite3.IntegrityError), db._connection() as connection:
            connection.execute("INSERT INTO conversation_messages (ticket_id, role, content, created_at) VALUES ('unknown', 'user', '', '')")
        self.assertEqual(self.state(ticket), state)

    def test_empty_oversized_unstarted_or_complete_replies_do_not_write(self):
        ticket = self.create(phone="")
        for reply in ("", "  ", "字" * 2001, None, "电话：演示电话002"):
            with self.assertRaises(ValueError):
                conversation.process_reply(ticket["id"], reply)
        self.assertEqual(self.state(ticket)["messages"], [])
        complete = self.create()
        with self.assertRaises(ValueError):
            conversation.start_conversation(complete["id"])
        self.assertIsNone(conversation.start_conversation("missing"))
        self.assertIsNone(conversation.process_reply("missing", "有效回复"))

    def test_statistics_use_observed_progress_not_all_complete_tickets(self):
        self.create()  # 原本就完整，不计入完成补全。
        self.create(phone="")  # 未开始，也计入待补全。
        first = self.start(phone="")
        second = self.start(customer_name="", phone="")
        conversation.process_reply(first["id"], "演示电话002")
        conversation.process_reply(second["id"], "姓名：演示甲")
        metrics = dashboard.get_dashboard_stats()
        self.assertEqual(metrics["incomplete_ticket_count"], 2)
        self.assertEqual(metrics["completed_conversation_count"], 1)
        self.assertEqual(metrics["avg_completed_fields"], 1)

    def test_empty_stats_have_no_invented_average(self):
        result = dashboard.get_dashboard_stats()
        self.assertEqual(result["incomplete_ticket_count"], 0)
        self.assertEqual(result["completed_conversation_count"], 0)
        self.assertIsNone(result["avg_completed_fields"])

    def test_page_start_reply_refresh_and_dashboard(self):
        ticket = self.create(phone="")
        app = self.app()
        self.assertTrue(any("当前完整度：80%" in item.value for item in app.markdown))
        self.assertEqual(self.state(ticket)["messages"], [])
        self.click(app, "开始补全")
        next(item for item in app.text_area if item.label == "客户回复").set_value("演示电话002")
        self.click(app, "提交回复")
        self.assertEqual(db.get_ticket(ticket["id"])["phone"], "演示电话002")
        self.assertTrue(any("当前完整度：100%" in item.value for item in app.markdown))
        self.assertEqual(next(item.value for item in app.metric if item.label == "已完成补全数量"), "1")
        state = self.state(ticket)
        app.run(timeout=30)
        self.assertEqual(self.state(ticket), state)
        self.sdk.assert_not_called()
        self.parser.assert_not_called()

    def test_page_real_ai_only_on_submit_and_not_on_start_or_refresh(self):
        self.enable_ai(phone="演示电话002")
        ticket = self.create(phone="")
        app = self.app()
        self.click(app, "开始补全")
        app.run(timeout=30)
        self.sdk.assert_not_called()
        next(item for item in app.text_area if item.label == "客户回复").set_value("演示电话002")
        self.click(app, "提交回复")
        self.client.responses.create.assert_called_once()
        app.run(timeout=30)
        self.client.responses.create.assert_called_once()
        self.assertEqual(len(db.get_all_tickets()), 1)

    def test_page_save_failure_retains_original_ticket_and_turn(self):
        ticket = self.start(phone="")
        state = self.state(ticket)
        app = self.app()
        self.block_event("field_filled")
        next(item for item in app.text_area if item.label == "客户回复").set_value("演示电话002")
        self.click(app, "提交回复")
        self.assertTrue(any("已回滚" in item.value for item in app.error))
        self.assertEqual(db.get_ticket(ticket["id"]), ticket)
        self.assertEqual(self.state(ticket), state)

    def test_negated_uncertain_and_placeholder_values_are_not_filled(self):
        for message in ("不要使用演示电话002", "可能是演示电话002", "演示电话002不对"):
            self.assertEqual(conversation.rule_parse_reply(message, ["phone"], "phone"), {})
            self.assertEqual(conversation.validate_reply_fields({"phone": "演示电话002"}, message, ["phone"]), {})
        for message in ("姓名：None", "姓名：未知", "地址：待补充", "预约时间：可能明天"):
            self.assertEqual(conversation.rule_parse_reply(message, list(COMPLETION_FIELDS), None), {})

    def test_concurrent_turn_saves_once_and_rejects_old_message_version(self):
        ticket = self.start(phone="", address="")
        last_id = self.state(ticket)["last_message_id"]
        def save():
            try:
                db.save_conversation_turn(ticket["id"], ticket, last_id, {"phone": "演示电话002"}, "演示电话002", "demo")
                return "saved"
            except ValueError:
                return "stale"
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(lambda _: save(), range(2))), ["saved", "stale"])
        self.assertEqual(len(self.history(ticket, "field_filled")), 1)
        self.assertEqual(len([row for row in self.state(ticket)["messages"] if row["role"] == "user"]), 1)

    def test_relative_appointment_uses_reply_event_day_for_later_priority_refresh(self):
        ticket = self.create(appointment_time="")
        with db._connection() as connection:
            connection.execute("UPDATE tickets SET created_time = ? WHERE id = ?", ("2026-10-01T10:00:00+08:00", ticket["id"]))
        conversation.start_conversation(ticket["id"])
        now = datetime(2026, 10, 13, 12, tzinfo=timezone(timedelta(hours=8)))
        with patch.object(events, "datetime", wraps=datetime) as event_clock, patch.object(priority, "datetime", wraps=datetime) as priority_clock:
            event_clock.now.return_value = now
            priority_clock.now.return_value = now
            saved = conversation.process_reply(ticket["id"], "预约时间：今天下午两点")
            self.assertEqual(saved["priority_suggested"], ticket["priority_suggested"])
            regenerated = generate_ticket_priority(ticket["id"], use_ai=False)
        self.assertEqual(regenerated["priority_suggested"], "高")
        self.assertIn("2026-10-13 14:00", regenerated["priority_reason"])
        self.assertEqual(regenerated["created_time"], "2026-10-01T10:00:00+08:00")

    def test_page_review_form_displays_filled_values_after_conversation(self):
        parsed = {field: {"value": value, "confidence": 0.95} for field, value in self.fields.items()}
        parsed["phone"] = {"value": "", "confidence": 0}
        ticket = create_ticket("缺电话的虚构需求", parsed)
        app = self.app()
        self.click(app, "开始补全")
        next(item for item in app.text_area if item.label == "客户回复").set_value("演示电话002")
        self.click(app, "提交回复")
        self.assertEqual(next(item.value for item in app.text_input if item.label == "电话"), "演示电话002")
        next(item for item in app.checkbox if item.label == "我已核对客户原始需求与字段").check()
        self.click(app, "审核通过并保存")
        self.assertEqual(db.get_ticket(ticket["id"])["phone"], "演示电话002")
        self.assertFalse(db.get_ticket(ticket["id"])["review_required"])


if __name__ == "__main__":
    unittest.main()
