"""处理建议闭环的离线测试；临时数据库、模拟 SDK，不访问真实 AI。"""

import copy
import json
import os
import sqlite3
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from openai import APIError, APITimeoutError
from streamlit.testing.v1 import AppTest

import config
from database import db
from modules import advisor, ai_client, ai_logging, ai_parser, dashboard, events
from modules.tickets import confirm_advice, confirm_priority, create_ticket, generate_ticket_advice


class AdvisorTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self._patch(patch.object(db, "DB_PATH", self.directory / "tickets.db"))
        self._patch(patch.object(ai_logging, "LOG_PATH", self.directory / "ai_calls.json"))
        self._patch(patch.object(config, "DEMO_MODE", True))
        self._patch(patch.object(ai_parser, "load_dotenv"))
        self._patch(patch.dict(os.environ, {"OPENAI_API_KEY": "offline-advisor-test-key"}))
        self.sdk = self._patch(patch.object(ai_client, "OpenAI"))
        self.client = self.sdk.return_value.__enter__.return_value
        self.parse_sdk = self._patch(patch.object(ai_parser, "OpenAI"))
        self.fields = {
            "service_type": "居家保洁", "appointment_time": "周六上午", "address": "虚构演示地址",
            "customer_name": "演示客户", "phone": "演示电话001", "urgency": "普通",
        }
        self.ticket = {"id": "advisor-example", "customer_input": "虚构原始需求", **self.fields,
                       "status": "待确认", "review_required": False, "created_time": "2026-10-13T10:00:00+08:00"}
        self.content = {"missing_information": [], "next_steps": ["联系客户核对服务范围", "核对预约时间"],
                        "attention_points": ["人工确认后执行。"]}

    def _patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def enable_ai(self):
        config.DEMO_MODE = False
        self.client.responses.create.return_value = SimpleNamespace(
            status="completed", output_text=json.dumps(self.content, ensure_ascii=False),
            usage=SimpleNamespace(total_tokens=73), model="test-advisor-model",
        )

    def create(self, **changes):
        return create_ticket("虚构需求", {**self.fields, **changes})

    def generate(self):
        return generate_ticket_advice(self.create()["id"])

    def final_text(self, ticket):
        return advisor.format_advice(json.loads(ticket["advice_json"]))

    def history(self, ticket_id, kind):
        return [item for item in events.get_ticket_events(ticket_id) if item["event_type"] == kind]

    def block_event(self, kind):
        self.assertIn(kind, ("advice_generated", "advice_reviewed"))
        with db._connection() as connection:
            connection.execute(f"""CREATE TRIGGER reject_advice BEFORE INSERT ON ticket_events
                WHEN NEW.event_type = '{kind}' BEGIN SELECT RAISE(ABORT, 'test rollback'); END""")

    def app(self):
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
        self.assertFalse(app.exception)
        return app

    def click(self, app, label):
        next(button for button in app.button if button.label == label).click().run(timeout=30)
        self.assertFalse(app.exception)

    def reason(self, app, value="运营核对后确认"):
        next(item for item in app.text_input if item.label == "处理建议评审原因").set_value(value)

    def test_fixed_output_shape_without_mutation_or_database_writes(self):
        original = copy.deepcopy(self.ticket)
        result = advisor.generate_advice(self.ticket)
        self.assertEqual(set(result), {"missing_information", "next_steps", "attention_points", "source"})
        self.assertEqual(result["source"], "demo")
        self.assertEqual(advisor.validate_advice(result), result)
        self.assertEqual(self.ticket, original)
        self.assertFalse(db.DB_PATH.exists())
        self.sdk.assert_not_called()
        self.parse_sdk.assert_not_called()

    def test_missing_phone_and_review_are_preserved_in_template(self):
        result = advisor.generate_advice({**self.ticket, "phone": "", "review_required": True})
        self.assertIn("需要补充联系电话", result["missing_information"])
        self.assertTrue(any("人工审核" in item for item in result["attention_points"]))

    def test_closed_ticket_template_does_not_restart_service(self):
        for status in ("已完成", "已取消"):
            result = advisor.generate_advice({**self.ticket, "status": status})
            self.assertTrue(any("核查工单" in item for item in result["next_steps"]))
            self.assertTrue(any("不自动重新启动" in item for item in result["attention_points"]))

    def test_demo_ticket_remains_offline_when_real_mode_enabled(self):
        self.enable_ai()
        for data in ({"priority_source": "demo"}, {"id": "DEMO-example"}):
            self.assertEqual(advisor.generate_advice({**self.ticket, **data})["source"], "demo")
        self.sdk.assert_not_called()
        self.assertEqual(ai_logging.read_ai_calls(), [])

    def test_real_sdk_success_schema_metadata_and_minimal_input(self):
        self.enable_ai()
        result = advisor.generate_advice(self.ticket)
        self.assertEqual(result, {**self.content, "source": "ai"})
        call = self.client.responses.create.call_args.kwargs
        self.assertTrue(call["text"]["format"]["strict"])
        self.assertFalse(call["text"]["format"]["schema"]["additionalProperties"])
        self.assertEqual(set(call["text"]["format"]["schema"]["required"]), set(self.content))
        self.assertFalse(call["store"])
        for field in ("customer_input", "customer_name", "address", "phone"):
            self.assertNotIn(self.ticket[field], call["input"][1]["content"])
        self.assertEqual(self.sdk.call_args.kwargs["max_retries"], 0)
        entry = ai_logging.read_ai_calls()[-1]
        self.assertEqual(entry["task_type"], "advisor")
        self.assertEqual(entry["status"], "success")
        self.assertEqual(entry["token_used"], 73)
        self.assertEqual(entry["model"], "test-advisor-model")
        self.assertGreaterEqual(entry["response_time"], 0)

    def test_api_error_and_timeout_fall_back_with_safe_logs(self):
        self.enable_ai()
        for error, status in (
            (APIError("private-error-details", request=SimpleNamespace(), body=None), "api_error"),
            (APITimeoutError(request=SimpleNamespace()), "timeout"),
            (RuntimeError("private-error-details"), "unexpected_error"),
        ):
            with self.subTest(status=status):
                self.client.responses.create.side_effect = error
                result = advisor.generate_advice(self.ticket)
                self.assertEqual(result["source"], "rule")
                self.assertIn("AI不可用，使用规则建议。", result["attention_points"])
                entry = ai_logging.read_ai_calls()[-1]
                self.assertEqual(entry["status"], status)
                self.assertNotIn("private-error-details", json.dumps(entry))

    def test_invalid_json_shape_or_incomplete_response_falls_back(self):
        self.enable_ai()
        invalid = ["not-json", "[]", "null", json.dumps({**self.content, "source": "ai"}),
                   json.dumps({**self.content, "next_steps": []}),
                   json.dumps({**self.content, "attention_points": "错误类型"}),
                   json.dumps({**self.content, "next_steps": [" "]}),
                   json.dumps({**self.content, "next_steps": [False]})]
        for output in invalid:
            with self.subTest(output=output):
                self.client.responses.create.return_value.output_text = output
                self.assertEqual(advisor.generate_advice(self.ticket)["source"], "rule")
                self.assertEqual(ai_logging.read_ai_calls()[-1]["status"], "invalid_response")
        self.client.responses.create.return_value.status = "incomplete"
        self.assertEqual(advisor.generate_advice(self.ticket)["source"], "rule")

    def test_missing_key_returns_rules_without_sdk_request(self):
        self.enable_ai()
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self.assertEqual(advisor.generate_advice(self.ticket)["source"], "rule")
        self.sdk.assert_not_called()
        self.assertFalse(ai_logging.read_ai_calls()[-1]["api_called"])

    def test_logging_failure_keeps_success_and_does_not_retry(self):
        self.enable_ai()
        with patch.object(ai_client, "append_ai_call", side_effect=OSError("test failure")):
            self.assertEqual(advisor.generate_advice(self.ticket)["source"], "ai")
        self.client.responses.create.assert_called_once()

    def test_ai_cannot_omit_known_missing_information(self):
        self.enable_ai()
        result = advisor.generate_advice({**self.ticket, "phone": "", "address": ""})
        self.assertIn("需要补充联系电话", result["missing_information"])
        self.assertIn("需要补充服务地址", result["missing_information"])

    def test_task_stats_do_not_mix_advisor_with_parse_or_priority(self):
        ai_logging.append_ai_call({"status": "success", "response_time": 1, "api_called": True})
        ai_logging.append_ai_call({"task_type": "priority", "status": "success", "response_time": 2, "api_called": True})
        before_parse, before_priority = ai_logging.get_ai_call_stats(), ai_logging.get_ai_call_stats("priority")
        self.enable_ai()
        advisor.generate_advice(self.ticket)
        self.client.responses.create.side_effect = RuntimeError("test failure")
        advisor.generate_advice(self.ticket)
        self.assertEqual(ai_logging.get_ai_call_stats(), before_parse)
        self.assertEqual(ai_logging.get_ai_call_stats("priority"), before_priority)
        self.assertEqual(ai_logging.get_ai_call_stats("advisor")["ai_call_count"], 2)
        self.assertEqual(ai_logging.get_ai_call_stats("advisor")["ai_success_rate"], 0.5)

    def test_creation_does_not_generate_advice(self):
        ticket = self.create()
        self.assertEqual(ticket["advice_decision"], "unreviewed")
        for field in set(db.ADVICE_FIELDS) - {"advice_decision"}:
            self.assertIsNone(ticket[field])
        self.assertEqual(self.history(ticket["id"], "advice_generated"), [])
        self.sdk.assert_not_called()

    def test_generation_saves_snapshot_and_system_event_without_other_changes(self):
        ticket = self.create(phone="")
        result = generate_ticket_advice(ticket["id"])
        self.assertEqual(db.get_ticket(ticket["id"]), result)
        self.assertEqual({k: v for k, v in result.items() if k not in db.ADVICE_FIELDS},
                         {k: v for k, v in ticket.items() if k not in db.ADVICE_FIELDS})
        self.assertEqual(result["advice_decision"], "unreviewed")
        self.assertEqual(json.loads(result["advice_json"])["source"], result["advice_source"])
        self.assertIsNotNone(datetime.fromisoformat(result["advice_generated_at"]).tzinfo)
        event = self.history(ticket["id"], "advice_generated")[0]
        self.assertEqual(event["actor_type"], "system")
        self.assertEqual(event["after_data"]["advice_json"], result["advice_json"])

    def test_acceptance_saves_full_original_advice_and_review_event(self):
        ticket = self.generate()
        text = self.final_text(ticket)
        saved = confirm_advice(ticket["id"], "accepted", text, "已核对客户需求")
        self.assertEqual(saved["advice_final"], text)
        self.assertEqual(saved["advice_decision"], "accepted")
        event = self.history(ticket["id"], "advice_reviewed")[0]
        self.assertEqual(event["actor_type"], "operator")
        self.assertEqual(event["before_data"]["advice_json"], ticket["advice_json"])
        self.assertEqual(event["after_data"]["advice_final"], text)
        self.assertEqual(event["reason"], "已核对客户需求")

    def test_modification_preserves_original_and_records_reason(self):
        ticket = self.generate()
        saved = confirm_advice(ticket["id"], "modified", "先核查原始记录，再联系客户确认", "按客户实际需求调整顺序")
        self.assertEqual(saved["advice_decision"], "modified")
        self.assertEqual(saved["advice_final"], "先核查原始记录，再联系客户确认")
        self.assertEqual(saved["advice_json"], ticket["advice_json"])
        self.assertEqual(self.history(ticket["id"], "advice_reviewed")[-1]["reason"], "按客户实际需求调整顺序")

    def test_rejection_clears_previous_final_but_retains_it_in_history(self):
        ticket = self.generate()
        text = self.final_text(ticket)
        confirm_advice(ticket["id"], "accepted", text, "首次确认")
        saved = confirm_advice(ticket["id"], "rejected", "", "客户需求变化，暂不采用")
        self.assertEqual(saved["advice_decision"], "rejected")
        self.assertIsNone(saved["advice_final"])
        self.assertEqual(saved["advice_json"], ticket["advice_json"])
        event = self.history(ticket["id"], "advice_reviewed")[-1]
        self.assertEqual(event["before_data"]["advice_final"], text)
        self.assertIsNone(event["after_data"]["advice_final"])

    def test_invalid_review_is_rejected_without_writes(self):
        ticket = self.generate()
        for decision, final, reason in (
            ("unreviewed", "方案", "原因"), ("modified", "方案", " "), ("accepted", "", "原因"),
            ("modified", " ", "原因"), ("rejected", "仍保存方案", "原因"), ("accepted", "改过的方案", "原因"),
            ("modified", self.final_text(ticket), "原因"), ("modified", None, "原因"),
        ):
            with self.subTest(decision=decision, final=final):
                with self.assertRaises(ValueError):
                    confirm_advice(ticket["id"], decision, final, reason)
                self.assertEqual(db.get_ticket(ticket["id"]), ticket)
        self.assertEqual(self.history(ticket["id"], "advice_reviewed"), [])

    def test_missing_ticket_or_advice_does_not_create_review(self):
        self.assertIsNone(generate_ticket_advice("missing"))
        self.assertIsNone(confirm_advice("missing", "rejected", "", "无效工单"))
        ticket = self.create()
        with self.assertRaisesRegex(ValueError, "先生成"):
            confirm_advice(ticket["id"], "modified", "方案", "原因")

    def test_duplicate_review_does_not_duplicate_events(self):
        ticket = self.generate()
        for _ in range(2):
            confirm_advice(ticket["id"], "accepted", self.final_text(ticket), "确认")
        self.assertEqual(len(self.history(ticket["id"], "advice_reviewed")), 1)

    def test_review_does_not_change_status_priority_or_ai_metrics(self):
        ticket = self.create(phone="")
        ticket = confirm_priority(ticket["id"], "高", "人工核对紧急需求")
        generated = generate_ticket_advice(ticket["id"])
        saved = confirm_advice(ticket["id"], "modified", "补齐信息后处理", "待补信息")
        for field in set(ticket) - set(db.ADVICE_FIELDS):
            self.assertEqual(saved[field], ticket[field])
        self.assertEqual(saved["advice_json"], generated["advice_json"])

    def test_reviewed_advice_cannot_be_regenerated(self):
        ticket = self.generate()
        saved = confirm_advice(ticket["id"], "accepted", self.final_text(ticket), "确认")
        with patch("modules.tickets.generate_advice") as generator:
            with self.assertRaisesRegex(ValueError, "已有人工评审"):
                generate_ticket_advice(ticket["id"])
            generator.assert_not_called()
        self.assertEqual(db.get_ticket(ticket["id"]), saved)

    def test_stale_generation_is_rejected_after_ticket_change(self):
        ticket = self.create()
        def during_request(snapshot):
            confirm_priority(ticket["id"], "高", "同步人工确认")
            return advisor.rule_advice(snapshot)
        with patch("modules.tickets.generate_advice", side_effect=during_request):
            with self.assertRaisesRegex(ValueError, "工单已被修改"):
                generate_ticket_advice(ticket["id"])
        self.assertIsNone(db.get_ticket(ticket["id"])["advice_json"])
        self.assertEqual(self.history(ticket["id"], "advice_generated"), [])

    def test_stale_review_snapshot_is_rejected(self):
        old = self.generate()
        current = generate_ticket_advice(old["id"])
        with self.assertRaisesRegex(ValueError, "工单已被修改"):
            confirm_advice(old["id"], "accepted", self.final_text(old), "确认", expected_ticket=old)
        self.assertEqual(db.get_ticket(old["id"]), current)

    def test_generation_event_failure_rolls_back_all_advice_fields(self):
        ticket = self.create()
        self.block_event("advice_generated")
        with self.assertRaises(sqlite3.IntegrityError):
            generate_ticket_advice(ticket["id"])
        self.assertEqual(db.get_ticket(ticket["id"]), ticket)
        self.assertEqual(self.history(ticket["id"], "advice_generated"), [])

    def test_review_event_failure_rolls_back_decision_and_final(self):
        ticket = self.generate()
        self.block_event("advice_reviewed")
        for decision, final in (("accepted", self.final_text(ticket)), ("modified", "修改的方案"), ("rejected", "")):
            with self.subTest(decision=decision):
                with self.assertRaises(sqlite3.IntegrityError):
                    confirm_advice(ticket["id"], decision, final, "运营原因")
                self.assertEqual(db.get_ticket(ticket["id"]), ticket)
        self.assertEqual(self.history(ticket["id"], "advice_reviewed"), [])

    def test_regeneration_failure_preserves_previous_advice(self):
        ticket = self.generate()
        self.block_event("advice_generated")
        with self.assertRaises(sqlite3.IntegrityError):
            generate_ticket_advice(ticket["id"])
        self.assertEqual(db.get_ticket(ticket["id"]), ticket)

    def test_migration_preserves_old_tickets_and_events_with_null_new_fields(self):
        ticket = self.create()
        old_events = events.get_ticket_events(ticket["id"])
        with db._connection() as connection:
            for field in db.ADVICE_FIELDS:
                connection.execute(f"ALTER TABLE tickets DROP COLUMN {field}")
            old = dict(connection.execute("SELECT * FROM tickets").fetchone())
        db.init_db()
        db.init_db()
        with db._connection() as connection:
            saved = dict(connection.execute("SELECT * FROM tickets").fetchone())
            columns = {row["name"]: row for row in connection.execute("PRAGMA table_info(tickets)")}
        self.assertEqual(saved, {**old, **dict.fromkeys(db.ADVICE_FIELDS)})
        self.assertEqual(columns["advice_decision"]["dflt_value"], "'unreviewed'")
        self.assertEqual(events.get_ticket_events(ticket["id"]), old_events)
        self.assertEqual(self.create()["advice_decision"], "unreviewed")
        self.assertEqual(dashboard.get_dashboard_stats()["advice_generated_count"], 0)

    def test_reinitialization_preserves_generated_and_reviewed_advice(self):
        ticket = self.generate()
        saved = confirm_advice(ticket["id"], "modified", "新的处理方案", "运营调整")
        db.init_db()
        db.init_db()
        self.assertEqual(db.get_ticket(ticket["id"]), saved)

    def test_dashboard_counts_only_generated_advice_by_latest_decision(self):
        self.create()
        tickets = [self.generate() for _ in range(4)]
        generate_ticket_advice(tickets[0]["id"])
        confirm_advice(tickets[1]["id"], "accepted", self.final_text(tickets[1]), "确认")
        confirm_advice(tickets[2]["id"], "modified", "调整方案", "调整原因")
        confirm_advice(tickets[3]["id"], "rejected", "", "不适用")
        stats = dashboard.get_dashboard_stats()
        self.assertEqual(stats["advice_generated_count"], 4)
        self.assertEqual(stats["advice_decision_counts"], {"未评审": 1, "已采纳": 1, "已修改": 1, "已拒绝": 1})

    def test_page_generation_is_explicit_and_refresh_does_not_call_ai(self):
        self.enable_ai()
        ticket = self.create()
        app = self.app()
        self.sdk.assert_not_called()
        self.click(app, "生成处理建议")
        self.client.responses.create.assert_called_once()
        self.assertEqual(db.get_ticket(ticket["id"])["advice_source"], "ai")
        history = events.get_ticket_events(ticket["id"])
        app.run(timeout=30)
        self.client.responses.create.assert_called_once()
        self.assertEqual(events.get_ticket_events(ticket["id"]), history)
        self.assertEqual(next(item.value for item in app.metric if item.label == "建议生成数量"), "1")

    def test_page_accept_modify_reject_and_event_history(self):
        ticket = self.generate()
        app = self.app()
        self.reason(app)
        self.click(app, "采纳建议")
        self.assertEqual(db.get_ticket(ticket["id"])["advice_decision"], "accepted")
        next(item for item in app.text_area if item.label == "编辑最终处理方案").set_value("先核对时间，再确认服务范围")
        self.click(app, "修改后采纳")
        self.assertTrue(any("必须填写原因" in item.value for item in app.warning))
        self.assertEqual(db.get_ticket(ticket["id"])["advice_decision"], "accepted")
        self.reason(app, "客户变更预约顺序")
        self.click(app, "修改后采纳")
        self.assertEqual(db.get_ticket(ticket["id"])["advice_final"], "先核对时间，再确认服务范围")
        self.reason(app, "方案不再适用")
        self.click(app, "拒绝建议")
        self.assertEqual(db.get_ticket(ticket["id"])["advice_decision"], "rejected")
        self.assertIsNone(db.get_ticket(ticket["id"])["advice_final"])
        history = next(item.value for item in app.table if "事件类型" in item.value.columns)
        self.assertIn("评审处理建议", history["事件类型"].tolist())
        app.run(timeout=30)
        self.assertEqual(len(self.history(ticket["id"], "advice_reviewed")), 3)
        self.sdk.assert_not_called()

    def test_page_rejects_stale_review_shown_before_other_operator_update(self):
        ticket = self.generate()
        app = self.app()
        changed = confirm_advice(ticket["id"], "modified", "另一个运营确认的方案", "先行处理")
        self.reason(app)
        self.click(app, "采纳建议")
        self.assertEqual(db.get_ticket(ticket["id"]), changed)
        self.assertTrue(any("工单已被修改" in item.value for item in app.warning))


if __name__ == "__main__":
    unittest.main()
