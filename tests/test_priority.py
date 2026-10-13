"""优先级规则、AI解释降级、人工确认、迁移和页面的离线测试。"""

import json
import os
import sqlite3
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from openai import APITimeoutError
from streamlit.testing.v1 import AppTest

import config
from database import db
from modules import ai_client, ai_logging, ai_parser, dashboard, events, priority
from modules.tickets import confirm_priority, create_ticket, generate_ticket_priority, review_ticket


class PriorityTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self._patch(patch.object(db, "DB_PATH", self.directory / "autoflow.db"))
        self._patch(patch.object(ai_logging, "LOG_PATH", self.directory / "ai_calls.json"))
        self._patch(patch.object(config, "DEMO_MODE", True))
        self._patch(patch.object(ai_parser, "load_dotenv"))
        self._patch(patch.dict(os.environ, {"OPENAI_API_KEY": "offline-priority-test-key"}))
        self.sdk = self._patch(patch.object(ai_client, "OpenAI"))
        self.client = self.sdk.return_value.__enter__.return_value
        self.now = datetime(2026, 10, 13, 10, tzinfo=priority.TIMEZONE)
        self.clock = self._patch(patch.object(priority, "datetime", wraps=datetime))
        self.clock.now.return_value = self.now
        self.fields = {
            "service_type": "居家保洁", "appointment_time": "2026-10-16 14:00",
            "address": "示例区演示小区", "customer_name": "演示客户",
            "phone": "演示电话001", "urgency": "普通",
        }
        self.ticket = {"id": "example", "customer_input": "预约保洁服务", **self.fields,
                       "created_time": self.now.isoformat(), "status": "待确认"}

    def _patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def enable_ai(self, level="普通", reason="预约安排明确，可按普通顺序处理。"):
        config.DEMO_MODE = False
        self.client.responses.create.return_value = SimpleNamespace(
            status="completed", output_text=json.dumps({"level": level, "reason": reason}, ensure_ascii=False),
            usage=SimpleNamespace(total_tokens=35),
        )

    def create(self, **changes):
        return create_ticket("预约虚构服务", {**self.fields, **changes})

    def history(self, ticket_id, event_type):
        return [item for item in events.get_ticket_events(ticket_id) if item["event_type"] == event_type]

    def reject_event(self, event_type):
        with db._connection() as connection:
            # 固定测试枚举，用 SQLite 真实失败验证事务回滚。
            connection.execute(
                "CREATE TRIGGER reject_priority BEFORE INSERT ON ticket_events "
                f"WHEN NEW.event_type = '{event_type}' "
                "BEGIN SELECT RAISE(ABORT, 'test priority failure'); END"
            )

    def test_output_has_exact_stable_fields_and_demo_source(self):
        for data in (self.ticket, {}, None, {**self.ticket, "urgency": "紧急"}):
            with self.subTest(data=data):
                result = priority.suggest_priority(data)
                self.assertEqual(set(result), {"level", "reason", "source"})
                self.assertIn(result["level"], priority.LEVELS)
                self.assertIsInstance(result["reason"], str)
                self.assertTrue(result["reason"])
                self.assertEqual(result["source"], "demo")
        self.sdk.assert_not_called()

    def test_explicit_urgent_request_is_high_even_without_phone(self):
        for data in ({"urgency": "紧急"}, {"customer_input": "请尽快安排加急服务"}):
            result = priority.rule_priority({**self.ticket, **data, "phone": ""})
            self.assertEqual(result["level"], "高")
        self.assertEqual(priority.rule_priority({**self.ticket, "phone": ""})["level"], "普通")

    def test_negated_urgency_and_risk_are_not_high(self):
        for text in ("不紧急，预约保洁", "不是紧急服务", "无需加急", "没有燃气泄漏，只是预约检查"):
            self.assertEqual(priority.rule_priority({**self.ticket, "customer_input": text})["level"], "普通")
        self.assertEqual(priority.rule_priority({**self.ticket, "customer_input": "不是紧急，但是需要高空作业"})["level"], "高")

    def test_explicit_high_risk_services_are_high(self):
        for service in priority.HIGH_RISK_TERMS:
            with self.subTest(service=service):
                result = priority.rule_priority({**self.ticket, "service_type": service, "phone": ""})
                self.assertEqual(result["level"], "高")
                self.assertIn(service, result["reason"])

    def test_age_or_nursing_alone_is_not_a_high_risk_rule(self):
        result = priority.rule_priority({**self.ticket, "service_type": "护理员上门", "customer_input": "照顾82岁老人"})
        self.assertEqual(result["level"], "普通")

    def test_near_appointment_includes_now_and_exactly_twenty_four_hours(self):
        for offset in (timedelta(0), timedelta(hours=2), timedelta(hours=24)):
            with self.subTest(offset=offset):
                value = (self.now + offset).isoformat()
                self.assertEqual(priority.rule_priority({**self.ticket, "appointment_time": value})["level"], "高")
        value = (self.now + timedelta(hours=24, minutes=1)).isoformat()
        self.assertEqual(priority.rule_priority({**self.ticket, "appointment_time": value})["level"], "普通")

    def test_relative_time_uses_original_creation_date_and_handles_chinese_time(self):
        data = {**self.ticket, "created_time": "2026-10-12T09:00:00+08:00", "appointment_time": "明天下午两点"}
        result = priority.rule_priority(data)
        self.assertEqual(result["level"], "高")
        self.assertIn("2026-10-13 14:00", result["reason"])
        self.assertEqual(priority.rule_priority({**self.ticket, "appointment_time": "2026年10月13日下午两点半"})["level"], "高")

    def test_past_appointment_requires_manual_classification(self):
        result = priority.rule_priority({**self.ticket, "appointment_time": "2026-10-12 09:00"})
        self.assertEqual(result["level"], "待人工分级")

    def test_ambiguous_or_invalid_time_is_not_guessed(self):
        for appointment in ("周六上午", "明天下午", "2026-02-30 14:00", "今天25点"):
            with self.subTest(appointment=appointment):
                result = priority.rule_priority({**self.ticket, "appointment_time": appointment})
                self.assertEqual(result["level"], "待人工分级")

    def test_explicit_flexible_appointment_field_is_low(self):
        self.assertEqual(priority.rule_priority({**self.ticket, "appointment_time": "时间不限"})["level"], "低")

    def test_low_requires_nonurgent_consultation_or_explicit_flexible_time(self):
        for text in ("咨询保洁收费", "保洁时间不限", "保洁不着急，没有时间要求"):
            self.assertEqual(priority.rule_priority({**self.ticket, "customer_input": text, "appointment_time": ""})["level"], "低")
        self.assertEqual(priority.rule_priority({**self.ticket, "appointment_time": ""})["level"], "待人工分级")

    def test_incomplete_information_can_still_have_high_urgency(self):
        self.assertEqual(priority.rule_priority({})["level"], "待人工分级")
        self.assertEqual(priority.rule_priority({"customer_input": "紧急，需要马上处理"})["level"], "高")

    def test_ai_only_explains_fixed_rule_level_and_logs_priority_task(self):
        self.enable_ai()
        result = priority.suggest_priority(self.ticket)
        self.assertEqual(result["level"], "普通")
        self.assertEqual(result["source"], "ai")
        self.assertIn("AI说明", result["reason"])
        request = self.client.responses.create.call_args.kwargs
        self.assertTrue(request["text"]["format"]["strict"])
        self.assertEqual(request["text"]["format"]["schema"]["properties"]["level"]["enum"], ["普通"])
        self.assertNotIn("演示电话", request["input"][1]["content"])
        entry = ai_logging.read_ai_calls()[0]
        self.assertEqual(entry["task_type"], "priority")
        self.assertEqual(entry["status"], "success")
        self.assertEqual(entry["token_used"], 35)
        self.assertEqual(ai_logging.get_ai_call_stats()["ai_call_count"], 0)
        self.assertEqual(ai_logging.get_ai_call_stats("priority")["ai_call_count"], 1)

    def test_ai_cannot_change_rule_level(self):
        self.enable_ai(level="低")
        result = priority.suggest_priority(self.ticket)
        self.assertEqual(result, priority.rule_priority(self.ticket))
        self.assertEqual(ai_logging.read_ai_calls()[0]["status"], "invalid_response")

    def test_ai_timeout_falls_back_without_exposing_raw_error(self):
        self.enable_ai()
        self.client.responses.create.side_effect = APITimeoutError(request=SimpleNamespace())
        self.assertEqual(priority.suggest_priority(self.ticket), priority.rule_priority(self.ticket))
        entry = ai_logging.read_ai_calls()[0]
        self.assertEqual(entry["status"], "timeout")
        self.assertNotIn("offline-priority-test-key", json.dumps(entry))

    def test_ai_exception_falls_back_without_crashing(self):
        self.enable_ai()
        self.client.responses.create.side_effect = RuntimeError("private test detail")
        self.assertEqual(priority.suggest_priority(self.ticket), priority.rule_priority(self.ticket))
        self.assertNotIn("private test detail", json.dumps(ai_logging.read_ai_calls()))

    def test_invalid_json_blank_explanation_and_incomplete_output_fall_back(self):
        self.enable_ai()
        for content in ("not-json", "[]", '{"level":"普通","reason":""}', '{"reason":"说明"}'):
            with self.subTest(content=content):
                self.client.responses.create.return_value.output_text = content
                self.assertEqual(priority.suggest_priority(self.ticket), priority.rule_priority(self.ticket))
        self.client.responses.create.return_value.status = "incomplete"
        self.assertEqual(priority.suggest_priority(self.ticket), priority.rule_priority(self.ticket))

    def test_missing_key_returns_rules_and_never_calls_sdk(self):
        config.DEMO_MODE = False
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self.assertEqual(priority.suggest_priority(self.ticket), priority.rule_priority(self.ticket))
        self.sdk.assert_not_called()

    def test_demo_ticket_remains_offline_when_real_mode_is_enabled(self):
        self.enable_ai()
        result = priority.suggest_priority({**self.ticket, "priority_source": "demo"})
        self.assertEqual(result["source"], "demo")
        self.sdk.assert_not_called()

    def test_logging_failure_does_not_retry_or_lose_valid_explanation(self):
        self.enable_ai()
        with patch.object(ai_client, "append_ai_call", side_effect=OSError("test")):
            self.assertEqual(priority.suggest_priority(self.ticket)["source"], "ai")
        self.client.responses.create.assert_called_once()

    def test_priority_calls_do_not_change_parse_success_metrics(self):
        for entry in ({"status": "success", "api_called": True, "response_time": 0.2},
                      {"task_type": "parse", "status": "timeout", "api_called": True, "response_time": 0.4},
                      {"task_type": "priority", "status": "success", "api_called": True, "response_time": 9.0}):
            ai_logging.append_ai_call(entry)
        stats = ai_logging.get_ai_call_stats()
        self.assertEqual(stats["ai_call_count"], 2)
        self.assertEqual(stats["ai_success_rate"], 0.5)
        self.assertAlmostEqual(stats["avg_response_time"], 0.3)

    def test_creation_generates_rule_suggestion_event_but_does_not_confirm(self):
        config.DEMO_MODE = False
        ticket = self.create(urgency="紧急")
        self.assertEqual(ticket["priority_suggested"], "高")
        self.assertEqual(ticket["priority_source"], "rule")
        self.assertIsNone(ticket["priority_final"])
        generated = self.history(ticket["id"], "priority_generated")
        self.assertEqual(len(generated), 1)
        self.assertEqual(generated[0]["actor_type"], "system")
        self.assertEqual(generated[0]["after_data"]["priority_suggested"], "高")
        self.sdk.assert_not_called()

    def test_confirm_and_override_save_final_and_event_reason(self):
        ticket = self.create(urgency="紧急")
        confirmed = confirm_priority(ticket["id"], "高", "确认紧急诉求")
        self.assertEqual(confirmed["priority_final"], "高")
        modified = confirm_priority(ticket["id"], "普通", "客户已改约，紧急情况解除")
        self.assertEqual(modified["priority_final"], "普通")
        self.assertEqual(modified["priority_suggested"], "高")
        self.assertEqual(modified["priority_reason"], ticket["priority_reason"])
        event = self.history(ticket["id"], "priority_confirmed")[-1]
        self.assertEqual(event["before_data"], {"priority_final": "高"})
        self.assertEqual(event["after_data"], {"priority_final": "普通"})
        self.assertEqual(event["reason"], "客户已改约，紧急情况解除")
        self.assertEqual(event["actor_type"], "operator")

    def test_confirm_requires_real_level_and_nonblank_reason(self):
        ticket = self.create()
        for level, reason in (("待人工分级", "确认"), ("高", " "), (None, "确认"), ("紧急", "确认")):
            with self.assertRaises(ValueError):
                confirm_priority(ticket["id"], level, reason)
        self.assertEqual(db.get_ticket(ticket["id"]), ticket)
        self.assertEqual(self.history(ticket["id"], "priority_confirmed"), [])

    def test_confirm_same_final_is_noop_and_unknown_ticket_returns_none(self):
        ticket = self.create()
        confirm_priority(ticket["id"], "高", "确认")
        confirm_priority(ticket["id"], "高", "重复确认")
        self.assertEqual(len(self.history(ticket["id"], "priority_confirmed")), 1)
        self.assertIsNone(confirm_priority("unknown", "高", "确认"))
        self.assertIsNone(generate_ticket_priority("unknown"))

    def test_high_priority_confirmation_does_not_bypass_review(self):
        fields = {field: {"value": value, "confidence": 0.9} for field, value in self.fields.items()}
        fields["phone"] = {"value": "", "confidence": 0}
        fields["urgency"] = {"value": "紧急", "confidence": 0.9}
        ticket = create_ticket("紧急但缺电话", fields)
        confirmed = confirm_priority(ticket["id"], "高", "先安排人工核对")
        self.assertTrue(confirmed["review_required"])
        self.assertEqual(confirmed["status"], "人工审核")
        self.assertEqual(dashboard.get_dashboard_stats()["high_priority_open_count"], 1)
        review_ticket(ticket["id"], self.fields)
        self.assertEqual(db.get_ticket(ticket["id"])["priority_final"], "高")

    def test_regeneration_preserves_final_and_original_parse_metadata(self):
        ticket = self.create()
        confirmed = confirm_priority(ticket["id"], "低", "确认时间灵活")
        with patch("modules.tickets.suggest_priority", return_value={"level": "高", "reason": "测试规则依据", "source": "rule"}):
            generated = generate_ticket_priority(ticket["id"])
        self.assertEqual(generated["priority_final"], "低")
        self.assertEqual(generated["priority_suggested"], "高")
        for field in ("confidence_score", "token_used", "response_time", "status", "review_required"):
            self.assertEqual(generated[field], confirmed[field])

    def test_stale_ai_result_cannot_overwrite_a_changed_ticket(self):
        ticket = self.create()
        def changed_during_call(snapshot):
            confirm_priority(ticket["id"], "高", "运营先行确认")
            return {"level": "低", "reason": "旧建议", "source": "ai"}
        with patch("modules.tickets.suggest_priority", side_effect=changed_during_call):
            with self.assertRaisesRegex(ValueError, "工单已被修改"):
                generate_ticket_priority(ticket["id"])
        stored = db.get_ticket(ticket["id"])
        self.assertEqual(stored["priority_final"], "高")
        self.assertEqual(stored["priority_suggested"], ticket["priority_suggested"])

    def test_confirmation_event_failure_rolls_back_final_level(self):
        ticket = self.create()
        self.reject_event("priority_confirmed")
        with self.assertRaises(sqlite3.IntegrityError):
            confirm_priority(ticket["id"], "高", "确认")
        self.assertEqual(db.get_ticket(ticket["id"]), ticket)
        self.assertEqual(self.history(ticket["id"], "priority_confirmed"), [])

    def test_generation_event_failure_rolls_back_suggestion_but_not_ticket_creation(self):
        db.init_db()
        self.reject_event("priority_generated")
        ticket = self.create()
        self.assertIsNone(ticket["priority_suggested"])
        self.assertEqual(db.get_ticket(ticket["id"]), ticket)
        self.assertEqual(len(self.history(ticket["id"], "ticket_created")), 1)
        with self.assertRaises(sqlite3.IntegrityError):
            generate_ticket_priority(ticket["id"], use_ai=False)
        self.assertEqual(db.get_ticket(ticket["id"]), ticket)

    def test_migration_adds_nullable_fields_and_preserves_existing_events(self):
        db.init_db()
        db.save_ticket(self.ticket)
        original_events = events.get_ticket_events(self.ticket["id"])
        with db._connection() as connection:
            for field in db.PRIORITY_FIELDS:
                connection.execute(f"ALTER TABLE tickets DROP COLUMN {field}")
            before = dict(connection.execute("SELECT * FROM tickets").fetchone())
        db.init_db()
        db.init_db()
        with db._connection() as connection:
            after = dict(connection.execute("SELECT * FROM tickets").fetchone())
        self.assertEqual(after, {**before, **dict.fromkeys(db.PRIORITY_FIELDS)})
        self.assertEqual(events.get_ticket_events(self.ticket["id"]), original_events)

    def test_regeneration_event_failure_preserves_previous_suggestion(self):
        ticket = self.create()
        self.reject_event("priority_generated")
        with patch("modules.tickets.suggest_priority", return_value={"level": "高", "reason": "新依据", "source": "rule"}):
            with self.assertRaises(sqlite3.IntegrityError):
                generate_ticket_priority(ticket["id"])
        self.assertEqual(db.get_ticket(ticket["id"]), ticket)

    def test_page_only_calls_ai_after_explicit_generation_and_preserves_final(self):
        self.enable_ai()
        ticket = self.create()
        confirm_priority(ticket["id"], "低", "运营已确认时间灵活")
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
        self.sdk.assert_not_called()
        next(button for button in app.button if button.label == "更新优先级建议").click().run(timeout=30)
        self.assertFalse(app.exception)
        self.client.responses.create.assert_called_once()
        stored = db.get_ticket(ticket["id"])
        self.assertEqual(stored["priority_source"], "ai")
        self.assertEqual(stored["priority_final"], "低")
        app.run(timeout=30)
        self.client.responses.create.assert_called_once()

    def test_dashboard_uses_only_final_priority_and_excludes_closed_tickets(self):
        pending = self.create(urgency="紧急")
        high = self.create()
        done = self.create()
        cancelled = self.create()
        for ticket in (high, done, cancelled):
            confirm_priority(ticket["id"], "高", "人工确认")
        db.update_ticket_status(high["id"], "异常")
        db.update_ticket_status(done["id"], "已完成")
        db.update_ticket_status(cancelled["id"], "已取消")
        stats = dashboard.get_dashboard_stats()
        self.assertEqual(stats["priority_counts"], {"高": 3, "普通": 0, "低": 0, "待分级": 1})
        self.assertEqual(stats["high_priority_open_count"], 1)
        self.assertIsNone(db.get_ticket(pending["id"])["priority_final"])

    def test_page_confirmation_manual_reason_and_refresh_behavior(self):
        ticket = self.create(urgency="紧急")
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
        self.assertFalse(app.exception)
        self.assertTrue(any(item.value == "优先级管理" for item in app.subheader))
        next(button for button in app.button if button.label == "确认建议").click().run(timeout=30)
        self.assertEqual(db.get_ticket(ticket["id"])["priority_final"], "高")
        self.assertEqual(next(metric.value for metric in app.metric if metric.label == "高优先级未完成工单"), "1")
        next(item for item in app.selectbox if item.label == "最终优先级").set_value("低")
        next(button for button in app.button if button.label == "修改等级").click().run(timeout=30)
        self.assertTrue(any("必须填写原因" in item.value for item in app.warning))
        self.assertEqual(db.get_ticket(ticket["id"])["priority_final"], "高")
        next(item for item in app.text_input if item.label == "确认／修改原因").set_value("客户明确改为非紧急服务")
        next(button for button in app.button if button.label == "修改等级").click().run(timeout=30)
        self.assertFalse(app.exception)
        self.assertEqual(db.get_ticket(ticket["id"])["priority_final"], "低")
        self.assertTrue(next(button for button in app.button if button.label == "确认建议").disabled)
        original = events.get_ticket_events(ticket["id"])
        app.run(timeout=30)
        self.assertEqual(events.get_ticket_events(ticket["id"]), original)
        self.sdk.assert_not_called()


if __name__ == "__main__":
    unittest.main()
