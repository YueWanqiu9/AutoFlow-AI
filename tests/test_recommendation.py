"""人员推荐解释的离线验证：固定规则对象、受约束输出、降级和只读性。"""

from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openai import APIError, APITimeoutError
from streamlit.testing.v1 import AppTest

import config
from database import db
from modules import ai_client, ai_logging, ai_parser, dashboard, matching, recommendation, staff
from modules.prompts import RECOMMENDATION_PROMPT


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self._patch(patch.object(db, "DB_PATH", self.directory / "autoflow.db"))
        self._patch(patch.object(ai_logging, "LOG_PATH", self.directory / "ai_calls.json"))
        self._patch(patch.object(config, "DEMO_MODE", True))
        self._patch(patch.object(ai_parser, "load_dotenv"))
        self._patch(patch.dict(os.environ, {"OPENAI_API_KEY": "offline-recommendation-test-key"}))
        self.sdk = self._patch(patch.object(ai_client, "OpenAI"))
        self.client = self.sdk.return_value.__enter__.return_value
        self.parser_sdk = self._patch(patch.object(ai_parser, "OpenAI"))
        self.ticket = {
            "id": "recommendation-test", "customer_input": "仅用于测试的私密原文",
            "service_type": "老人护理", "address": "演示甲区虚构小区123号",
            "appointment_time": "测试预约原文", "customer_name": "测试客户姓名",
            "phone": "测试客户电话", "status": "待确认", "review_required": False,
            "created_time": "2026-10-14T10:00:00+08:00", "priority_final": "高",
        }
        self.person = staff.create_staff("测试护理员（虚构）", "测试人员电话", ["老人护理"], "演示甲区")
        self.candidates = matching.match_staff(self.ticket)

    def _patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def response(self, content, *, status="completed"):
        return SimpleNamespace(status=status, output_text=json.dumps(content, ensure_ascii=False),
                               usage=SimpleNamespace(total_tokens=87), model="test-recommendation-model")

    def valid_content(self, context):
        return {"recommended_staff_id": context["fixed_staff_id"],
                "reason": context["allowed_reasons"][:], "risk_notes": context["allowed_risk_notes"][:1]}

    def enable_ai(self, transform=None):
        config.DEMO_MODE = False

        def respond(**kwargs):
            context = json.loads(kwargs["input"][1]["content"])
            content = self.valid_content(context)
            return self.response(transform(content, context) if transform else content)

        self.client.responses.create.side_effect = respond

    def generate(self, ticket=None, candidates=None):
        return recommendation.generate_recommendation_explanation(
            self.ticket if ticket is None else ticket, self.candidates if candidates is None else candidates,
        )

    def snapshot_database(self):
        with db._connection() as connection:
            return "\n".join(connection.iterdump())

    def open_app(self):
        db.save_ticket(self.ticket)
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
        self.assertFalse(app.exception)
        return app

    def click(self, app, label="生成AI推荐说明"):
        next(item for item in app.button if item.label == label).click().run(timeout=30)
        self.assertFalse(app.exception)

    def test_demo_has_fixed_public_shape_and_never_calls_ai(self):
        result = self.generate()
        self.assertEqual(set(result), {"recommended_staff", "reason", "risk_notes", "source"})
        self.assertEqual(result["recommended_staff"], self.person["name"])
        self.assertEqual(result["source"], "rule")
        self.assertTrue(any("演示模式" in note for note in result["risk_notes"]))
        self.assertIn(recommendation.SCHEDULE_RISK, result["risk_notes"])
        self.sdk.assert_not_called()
        self.assertEqual(ai_logging.read_ai_calls(), [])

    def test_api_input_preserves_candidates_and_omits_private_fields(self):
        self.enable_ai()
        result = self.generate()
        self.assertEqual(result["source"], "ai")
        call = self.client.responses.create.call_args.kwargs
        context = json.loads(call["input"][1]["content"])
        self.assertEqual(context["candidates"], self.candidates)
        self.assertEqual(context["fixed_staff_id"], self.candidates[0]["staff_id"])
        self.assertEqual(context["rule_version"], "4B-1.1")
        self.assertEqual(context["ticket"]["required_skill"], "老人护理")
        for private in (self.ticket["customer_input"], self.ticket["customer_name"], self.ticket["address"],
                        self.ticket["phone"], self.ticket["appointment_time"], self.person["phone"], self.person["name"]):
            self.assertNotIn(private, call["input"][1]["content"])
            self.assertNotIn(private, json.dumps(ai_logging.read_ai_calls(), ensure_ascii=False))
        self.assertEqual(call["input"][0]["content"], RECOMMENDATION_PROMPT)

    def test_schema_fixes_person_and_limits_text_to_supported_statements(self):
        self.enable_ai()
        self.generate()
        call = self.client.responses.create.call_args.kwargs
        context = json.loads(call["input"][1]["content"])
        fmt = call["text"]["format"]
        self.assertTrue(fmt["strict"])
        self.assertEqual(fmt["type"], "json_schema")
        self.assertFalse(fmt["schema"]["additionalProperties"])
        self.assertEqual(set(fmt["schema"]["required"]), {"recommended_staff_id", "reason", "risk_notes"})
        self.assertEqual(fmt["schema"]["properties"]["recommended_staff_id"]["enum"], [self.person["id"]])
        self.assertEqual(fmt["schema"]["properties"]["reason"]["items"]["enum"], context["allowed_reasons"])
        self.assertEqual(fmt["schema"]["properties"]["risk_notes"]["items"]["enum"], context["allowed_risk_notes"])
        self.assertNotIn("score", fmt["schema"]["properties"])
        self.assertNotIn("tools", call)
        self.assertFalse(call["store"])
        self.assertEqual(self.sdk.call_args.kwargs["timeout"], 15.0)
        self.assertEqual(self.sdk.call_args.kwargs["max_retries"], 0)

    def test_ai_can_organize_existing_reasons_and_mandatory_risks_are_restored(self):
        self.enable_ai(lambda content, _: {**content, "reason": list(reversed(content["reason"])), "risk_notes": []})
        result = self.generate()
        context = json.loads(self.client.responses.create.call_args.kwargs["input"][1]["content"])
        self.assertEqual(result["reason"], list(reversed(context["allowed_reasons"])))
        self.assertEqual(result["risk_notes"], context["allowed_risk_notes"])
        self.assertEqual(result["recommended_staff"], self.person["name"])

    def test_api_failure_timeout_and_unexpected_error_return_rule_fallback(self):
        self.enable_ai()
        for error, expected in (
            (APIError("private-api-error", request=SimpleNamespace(), body=None), "api_error"),
            (APITimeoutError(request=SimpleNamespace()), "timeout"),
            (RuntimeError("private-runtime-error"), "unexpected_error"),
        ):
            with self.subTest(expected=expected):
                self.client.responses.create.side_effect = error
                result = self.generate()
                self.assertEqual(result["source"], "rule")
                self.assertEqual(result["recommended_staff"], self.person["name"])
                self.assertIn("AI不可用", result["risk_notes"][0])
                entry = ai_logging.read_ai_calls()[-1]
                self.assertEqual(entry["status"], expected)
                self.assertNotIn("private-", json.dumps(entry))
                self.assertNotIn("offline-recommendation-test-key", json.dumps(entry))

    def test_bad_json_refusal_and_incomplete_responses_fall_back(self):
        self.enable_ai()
        self.client.responses.create.side_effect = None
        for content, status in (("not-json", "completed"), ("", "completed"), ("[]", "completed"), ("{}", "incomplete")):
            with self.subTest(content=content, status=status):
                self.client.responses.create.return_value = SimpleNamespace(status=status, output_text=content)
                self.assertEqual(self.generate()["source"], "rule")
                self.assertEqual(ai_logging.read_ai_calls()[-1]["status"], "invalid_response")

    def test_missing_or_extra_fields_invalid_lists_and_blank_reasons_rejected(self):
        transforms = (
            lambda content, _: {key: value for key, value in content.items() if key != "reason"},
            lambda content, _: {**content, "source": "ai"},
            lambda content, _: {**content, "reason": []},
            lambda content, _: {**content, "reason": "技能匹配"},
            lambda content, _: {**content, "reason": [""]},
            lambda content, _: {**content, "risk_notes": None},
            lambda content, _: {**content, "risk_notes": [1]},
            lambda content, _: {**content, "reason": content["reason"] * 20},
        )
        for transform in transforms:
            with self.subTest(transform=transform):
                self.enable_ai(transform)
                self.assertEqual(self.generate()["source"], "rule")
                self.assertEqual(ai_logging.read_ai_calls()[-1]["status"], "invalid_response")

    def test_model_cannot_choose_another_candidate_or_non_candidate(self):
        second = staff.create_staff("另一位测试人员（虚构）", skills=["助浴"], service_area="演示甲区")
        self.candidates = matching.match_staff(self.ticket)
        for staff_id in (second["id"], 999, True, str(self.person["id"])):
            with self.subTest(staff_id=staff_id):
                self.enable_ai(lambda content, _: {**content, "recommended_staff_id": staff_id})
                result = self.generate()
                self.assertEqual(result["source"], "rule")
                self.assertEqual(result["recommended_staff"], self.person["name"])
                self.assertEqual(ai_logging.read_ai_calls()[-1]["status"], "invalid_response")

    def test_model_cannot_return_score_or_assignment_fields(self):
        for extra in ({"score": 99}, {"staff_status": "busy"}, {"assigned": True}, {"recommended_staff": "模型另选姓名"}):
            with self.subTest(extra=extra):
                self.enable_ai(lambda content, _: {**content, **extra})
                self.assertEqual(self.generate()["source"], "rule")
                self.assertEqual(staff.get_staff(self.person["id"])["status"], "available")
                self.assertEqual(matching.match_staff(self.ticket), self.candidates)

    def test_unsupported_claims_are_rejected_even_if_json_is_valid(self):
        for claim in ("已完成派单。", "该人员一定可用。", "距离客户只有一公里。", "拥有十年护理经验。", "评分应改为99分。"):
            for field in ("reason", "risk_notes"):
                with self.subTest(claim=claim, field=field):
                    self.enable_ai(lambda content, _: {**content, field: [claim]})
                    result = self.generate()
                    self.assertEqual(result["source"], "rule")
                    self.assertNotIn(claim, result["reason"] + result["risk_notes"])

    def test_busy_partial_and_unknown_area_risks_cannot_be_omitted(self):
        staff.update_staff(self.person["id"], skills=["助浴"], service_area="演示乙区")
        staff.update_staff_status(self.person["id"], "busy")
        self.candidates = matching.match_staff(self.ticket)
        self.enable_ai(lambda content, _: {**content, "risk_notes": []})
        result = self.generate()
        self.assertEqual(result["source"], "ai")
        for phrase in ("忙碌", "部分相关", "服务区域未匹配", "未接入实时排班"):
            self.assertTrue(any(phrase in note for note in result["risk_notes"]))
        self.assertFalse(any("完全匹配" in reason or "登记状态为可用" in reason for reason in result["reason"]))

    def test_missing_information_review_and_closed_ticket_risks_are_preserved(self):
        for status in ("人工审核", "已完成", "已取消"):
            with self.subTest(status=status):
                ticket = {**self.ticket, "appointment_time": "", "address": "", "status": status, "review_required": True}
                result = self.generate(ticket, matching.match_staff(ticket))
                for phrase in ("预约时间", "服务地址", "人工审核"):
                    self.assertTrue(any(phrase in note for note in result["risk_notes"]))
                if status != "人工审核":
                    self.assertTrue(any("工单已结束" in note for note in result["risk_notes"]))

    def test_no_candidates_never_invents_person_or_calls_ai(self):
        self.enable_ai()
        result = self.generate(candidates=[])
        self.assertEqual(result["recommended_staff"], "")
        self.assertEqual(result["source"], "rule")
        self.assertTrue(result["reason"])
        self.sdk.assert_not_called()

    def test_invalid_or_reordered_candidate_input_is_not_silently_rewritten(self):
        second = staff.create_staff("另一位测试人员（虚构）", skills=["助浴"], service_area="演示甲区")
        self.candidates = matching.match_staff(self.ticket)
        invalid = (
            list(reversed(self.candidates)), [self.candidates[0], self.candidates[0]],
            [{**self.candidates[0], "score": 99}], [{**self.candidates[0], "score": True}],
            [{**self.candidates[0], "match_details": ["伪造规则理由"]}],
            [{**self.candidates[0], "staff_id": 999}], [{"staff_id": second["id"]}],
        )
        self.enable_ai()
        for candidates in invalid:
            with self.subTest(candidates=candidates):
                before = deepcopy(candidates)
                result = self.generate(candidates=candidates)
                self.assertEqual(result["recommended_staff"], "")
                self.assertEqual(result["source"], "rule")
                self.assertEqual(candidates, before)
        self.sdk.assert_not_called()

    def test_current_profile_mismatch_requires_refresh_and_does_not_rescore_input(self):
        self.enable_ai()
        original = deepcopy(self.candidates)
        staff.update_staff_status(self.person["id"], "busy")
        result = self.generate()
        self.assertEqual(result["recommended_staff"], "")
        self.assertEqual(self.candidates, original)
        self.assertTrue(any("最新人员推荐" in note for note in result["risk_notes"]))
        self.sdk.assert_not_called()

    def test_profile_read_failure_falls_back_without_bubbling_up(self):
        self.enable_ai()
        with patch.object(recommendation, "list_staff", side_effect=sqlite3.OperationalError("private-db-error")):
            result = self.generate()
        self.assertEqual(result["source"], "rule")
        self.assertNotIn("private-db-error", json.dumps(result))
        self.sdk.assert_not_called()

    def test_invalid_public_arguments_return_fixed_fallback_shape(self):
        for ticket, candidates in ((None, self.candidates), (self.ticket, None), (self.ticket, "[]")):
            result = recommendation.generate_recommendation_explanation(ticket, candidates)
            self.assertEqual(set(result), {"recommended_staff", "reason", "risk_notes", "source"})
            self.assertEqual(result["source"], "rule")

    def test_demo_tickets_and_demo_candidates_stay_offline_in_real_mode(self):
        self.enable_ai()
        for changes in ({"id": "DEMO-example"}, {"priority_source": "demo"}):
            result = self.generate({**self.ticket, **changes})
            self.assertEqual(result["source"], "rule")
        demo_person = staff.create_staff("demo测试人员（虚构）", skills=["老人护理"], service_area="演示甲区", is_demo=True)
        self.candidates = matching.match_staff(self.ticket)
        self.assertEqual(self.generate()["source"], "rule")
        self.assertIn(demo_person["id"], [item["staff_id"] for item in self.candidates])
        self.sdk.assert_not_called()

    def test_missing_key_and_configuration_failure_use_rule_fallback(self):
        self.enable_ai()
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            self.assertEqual(self.generate()["source"], "rule")
        self.sdk.assert_not_called()
        self.assertEqual(ai_logging.read_ai_calls()[-1]["status"], "configuration_error")
        self.assertFalse(ai_logging.read_ai_calls()[-1]["api_called"])
        with patch.object(ai_client, "get_ai_status", side_effect=RuntimeError("private-config-error")):
            self.assertEqual(self.generate()["source"], "rule")
        self.assertNotIn("private-config-error", json.dumps(ai_logging.read_ai_calls()))

    def test_logging_failure_preserves_valid_explanation_without_retry(self):
        self.enable_ai()
        with patch.object(ai_client, "append_ai_call", side_effect=OSError("test log failure")):
            result = self.generate()
        self.assertEqual(result["source"], "ai")
        self.client.responses.create.assert_called_once()

    def test_logs_have_separate_task_metadata_and_no_success_for_invalid_output(self):
        self.enable_ai()
        self.generate()
        entry = ai_logging.read_ai_calls()[-1]
        self.assertEqual(entry["task_type"], "recommendation")
        self.assertEqual(entry["status"], "success")
        self.assertTrue(entry["api_called"])
        self.assertEqual(entry["token_used"], 87)
        self.assertEqual(entry["model"], "test-recommendation-model")
        self.assertGreaterEqual(entry["response_time"], 0)
        self.assertTrue(entry["timestamp"])
        self.enable_ai(lambda content, _: {**content, "reason": ["一定可用"]})
        self.generate()
        entry = ai_logging.read_ai_calls()[-1]
        self.assertEqual(entry["status"], "invalid_response")
        self.assertEqual(entry["output"], {})

    def test_fifth_task_does_not_change_four_existing_ai_statistics(self):
        task_types = ("parse", "priority", "advisor", "conversation")
        for task_type in task_types:
            ai_logging.append_ai_call({"task_type": task_type, "api_called": True, "status": "success", "response_time": 2.0})
        before = {task: ai_logging.get_ai_call_stats(task) for task in task_types}
        self.enable_ai()
        self.generate()
        self.client.responses.create.side_effect = RuntimeError("test error")
        self.generate()
        self.assertEqual({task: ai_logging.get_ai_call_stats(task) for task in task_types}, before)
        self.assertEqual(ai_logging.get_ai_call_stats("recommendation")["ai_call_count"], 2)
        self.assertEqual(ai_logging.get_ai_call_stats("recommendation")["ai_success_rate"], 0.5)

    def test_dashboard_has_no_new_business_metrics_or_changed_values(self):
        db.save_ticket(self.ticket)
        before = dashboard.get_dashboard_stats()
        self.enable_ai()
        self.generate()
        self.assertEqual(dashboard.get_dashboard_stats(), before)

    def test_success_and_failure_leave_schema_tickets_staff_and_events_unchanged(self):
        db.save_ticket(self.ticket)
        before = self.snapshot_database()
        self.enable_ai()
        self.assertEqual(self.generate()["source"], "ai")
        self.client.responses.create.side_effect = RuntimeError("test failure")
        self.assertEqual(self.generate()["source"], "rule")
        self.assertEqual(self.snapshot_database(), before)
        self.assertEqual(staff.get_staff(self.person["id"]), self.person)
        self.assertEqual(matching.match_staff(self.ticket), self.candidates)

    def test_input_scores_order_ticket_and_staff_snapshot_are_not_mutated(self):
        self.enable_ai()
        people = staff.list_staff()
        before = deepcopy((self.ticket, self.candidates, people))
        result = recommendation.generate_recommendation_explanation(self.ticket, self.candidates, staff_pool=people)
        result["reason"].append("修改返回值不影响原数据")
        self.assertEqual((self.ticket, self.candidates, people), before)

    def test_mutated_client_context_does_not_change_callers_or_weaken_local_check(self):
        self.enable_ai()
        before = deepcopy((self.ticket, self.candidates))

        def bad_client(context, ticket_id):
            context["candidates"][0]["score"] = 1
            context["allowed_reasons"].append("虚构已经派单")
            return {"recommended_staff_id": self.person["id"], "reason": ["虚构已经派单"], "risk_notes": []}

        with patch.object(recommendation, "request_recommendation", side_effect=bad_client):
            result = self.generate()
        self.assertEqual(result["source"], "rule")
        self.assertEqual((self.ticket, self.candidates), before)
        self.assertNotIn("虚构已经派单", result["reason"])

    def test_page_view_and_refresh_do_not_call_explanation_sdk(self):
        self.enable_ai()
        app = self.open_app()
        self.click(app, "查看推荐")
        self.sdk.assert_not_called()
        app.run(timeout=30)
        self.assertFalse(app.exception)
        self.sdk.assert_not_called()
        self.parser_sdk.assert_not_called()

    def test_page_explicit_button_displays_name_reasons_risks_and_source(self):
        self.enable_ai()
        app = self.open_app()
        before = self.snapshot_database()
        self.click(app)
        self.assertTrue(any(item.value == "AI推荐说明" for item in app.subheader))
        self.assertTrue(any(f"推荐人员：{self.person['name']}" in item.value for item in app.markdown))
        self.assertTrue(any(recommendation.SCHEDULE_RISK in item.value for item in app.markdown))
        self.assertTrue(any(item.value == "来源：AI" for item in app.caption))
        self.assertTrue(any("评分：100 / 100" in item.value for item in app.markdown))
        self.client.responses.create.assert_called_once()
        app.run(timeout=30)
        self.assertFalse(app.exception)
        self.client.responses.create.assert_called_once()
        self.assertFalse(any(item.value == "AI推荐说明" for item in app.subheader))
        self.assertEqual(self.snapshot_database(), before)

    def test_page_api_failure_keeps_rule_candidates_and_explanation(self):
        self.enable_ai()
        self.client.responses.create.side_effect = RuntimeError("private-test-failure")
        app = self.open_app()
        self.click(app)
        self.assertTrue(any("第 1 名：" in item.value for item in app.markdown))
        self.assertTrue(any("AI不可用" in item.value for item in app.markdown))
        self.assertTrue(any(item.value == "来源：规则" for item in app.caption))
        self.assertTrue(any(item.label == "确认派单" for item in app.button))
        self.assertFalse(any(item.label == "自动派单" for item in app.button))
        with db._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0], 0)

    def test_page_no_candidates_never_calls_sdk(self):
        self.enable_ai()
        staff.update_staff_status(self.person["id"], "inactive")
        app = self.open_app()
        self.click(app)
        self.assertTrue(any("暂无匹配人员" in item.value for item in app.info))
        self.sdk.assert_not_called()

    def test_page_recalculates_before_explanation_and_clears_on_ticket_switch(self):
        self.enable_ai()
        app = self.open_app()
        self.click(app, "查看推荐")
        staff.update_staff_status(self.person["id"], "busy")
        self.click(app)
        self.assertTrue(any("评分：85 / 100" in item.value for item in app.markdown))
        self.assertTrue(any("登记状态为忙碌" in item.value for item in app.markdown))
        other = {**self.ticket, "id": "other", "service_type": "居家保洁"}
        db.save_ticket(other)
        app.run(timeout=30)
        next(item for item in app.selectbox if item.label == "选择工单").set_value("other").run(timeout=30)
        self.assertFalse(app.exception)
        self.assertFalse(any(item.value == "AI推荐说明" for item in app.subheader))
        self.client.responses.create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
