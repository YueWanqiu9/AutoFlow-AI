"""需求解析的离线回归检查；模型调用、.env 读取和数据库写入均已替换。"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    RateLimitError,
)

import config
from modules import ai_logging, ai_parser
from modules.tickets import create_ticket


class AIParserTests(unittest.TestCase):
    def setUp(self):
        self._patch(patch.dict(os.environ, {"OPENAI_API_KEY": "offline-test-key"}, clear=True))
        self._patch(patch.object(config, "DEMO_MODE", False))
        self.load_dotenv = self._patch(patch.object(ai_parser, "load_dotenv"))
        self.sdk = self._patch(patch.object(ai_parser, "OpenAI"))
        self.save_ticket = self._patch(patch("modules.tickets.save_ticket"))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.log_path = Path(directory.name) / "ai_calls.json"
        self._patch(patch.object(ai_logging, "LOG_PATH", self.log_path))
        self.client = self.sdk.return_value.__enter__.return_value
        values = {
            "service_type": "空调清洗",
            "appointment_time": "周六上午",
            "address": "示例路1号",
            "customer_name": "演示客户",
            "phone": "演示电话001",
            "urgency": "紧急",
        }
        self.fields = {
            field: {"value": value, "confidence": 0.95}
            for field, value in values.items()
        }
        self.text = "我叫演示客户，预约周六上午空调清洗，地址示例路1号，电话演示电话001，请尽快。"
        self.set_response(self.fields)

    def _patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def set_response(self, fields, *, status="completed", output=None):
        self.client.responses.create.return_value = SimpleNamespace(
            status=status,
            output_text=json.dumps(fields, ensure_ascii=False),
            output=output or [],
        )

    def assert_fallback(self, status="invalid_response", *, api_called=True):
        result = ai_parser.parse_customer_request(self.text)
        self.assertEqual(
            result,
            {field: {"value": "", "confidence": 0.0} for field in self.fields},
        )
        self.assertIsInstance(result, ai_parser.ParsedRequest)
        self.assertEqual(result.call_status, status)
        self.assertTrue(result.error)
        self.assertIs(result.api_called, api_called)
        if api_called:
            self.assertIsInstance(result.response_time, float)
            self.assertGreaterEqual(result.response_time, 0)
            self.assertIsNotNone(result.request_time)
        else:
            self.assertIsNone(result.response_time)
            self.assertIsNone(result.request_time)
            self.assertIsNone(result.response_timestamp)
        return result

    def assert_demo(self, result, fields=None):
        self.assertEqual(
            result,
            fields if fields is not None
            else {field: {"value": "", "confidence": 0.0} for field in self.fields},
        )
        self.assertEqual(result.call_status, "demo")
        self.assertEqual(result.model, "demo-parser")
        self.assertEqual(result.error, "")
        self.assertFalse(result.api_called)
        self.assertEqual(result.token_used, 0)
        self.assertIsNone(result.response_time)
        self.assertIsNone(result.request_time)
        self.assertIsNone(result.response_timestamp)

    def assert_invalid_response_is_rejected(self, status="invalid_response"):
        with self.assertRaises(ai_parser.AIParserError):
            ai_parser._parse_json(self.client.responses.create.return_value.output_text)
        return self.assert_fallback(status)

    def test_success_uses_strict_schema_and_separate_user_input(self):
        self.assertEqual(ai_parser.parse_customer_request(self.text), self.fields)
        self.sdk.assert_called_once_with(
            api_key="offline-test-key", timeout=30.0, max_retries=0
        )
        self.client.responses.create.assert_called_once()
        kwargs = self.client.responses.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "gpt-4o-mini")
        self.assertFalse(kwargs["store"])
        self.assertGreater(kwargs["max_output_tokens"], 0)
        self.assertLessEqual(kwargs["max_output_tokens"], 1000)
        messages = kwargs["input"]
        self.assertEqual([message["role"] for message in messages], ["system", "user"])
        self.assertEqual(messages[1]["content"], self.text)
        response_format = kwargs["text"]["format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["strict"])
        schema = response_format["schema"]
        self.assertEqual(schema["type"], "object")
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(self.fields))
        self.assertEqual(set(schema["properties"]), set(self.fields))
        for field_schema in schema["properties"].values():
            self.assertEqual(field_schema["type"], "object")
            self.assertFalse(field_schema["additionalProperties"])
            self.assertEqual(set(field_schema["required"]), {"value", "confidence"})
            self.assertEqual(set(field_schema["properties"]), {"value", "confidence"})
            self.assertEqual(field_schema["properties"]["value"], {"type": "string"})
            self.assertEqual(
                field_schema["properties"]["confidence"],
                {"type": "number", "minimum": 0, "maximum": 1},
            )

    def test_dotenv_uses_project_root_and_preserves_environment(self):
        ai_parser.parse_customer_request(self.text)
        self.load_dotenv.assert_called_once()
        args, kwargs = self.load_dotenv.call_args
        dotenv_path = args[0] if args else kwargs["dotenv_path"]
        self.assertEqual(Path(dotenv_path), Path(__file__).resolve().parents[1] / ".env")
        self.assertIs(kwargs["override"], False)

    def test_model_can_be_configured_by_environment(self):
        os.environ["OPENAI_MODEL"] = "offline-test-model"
        ai_parser.parse_customer_request(self.text)
        self.assertEqual(
            self.client.responses.create.call_args.kwargs["model"], "offline-test-model"
        )

    def test_strings_are_trimmed_and_unknown_fields_remain_empty(self):
        fields = {
            key: {**item, "value": f"  {item['value']}  "}
            for key, item in self.fields.items()
        }
        fields["phone"] = {"value": " \t ", "confidence": 0.99}
        self.set_response(fields)
        expected = {**self.fields, "phone": {"value": "", "confidence": 0.0}}
        self.assertEqual(ai_parser.parse_customer_request(self.text), expected)

    def test_confidence_boundaries_are_accepted_as_floats(self):
        for confidence in (0, 1, 0.0, 0.8, 1.0):
            with self.subTest(confidence=confidence):
                self.set_response(
                    {**self.fields, "phone": {"value": "演示电话001", "confidence": confidence}}
                )
                parsed = ai_parser.parse_customer_request(self.text)
                self.assertEqual(parsed["phone"]["confidence"], float(confidence))
                self.assertIsInstance(parsed["phone"]["confidence"], float)

    def test_blank_or_non_string_input_never_calls_api(self):
        for text in ("", " \t\n ", None, 123):
            with self.subTest(text=text), self.assertRaises(ValueError):
                ai_parser.parse_customer_request(text)
        self.sdk.assert_not_called()
        self.assertFalse(self.log_path.exists())

    def test_missing_or_blank_key_never_calls_api(self):
        for value in (None, "", " \t "):
            with self.subTest(key=value):
                if value is None:
                    os.environ.pop("OPENAI_API_KEY", None)
                else:
                    os.environ["OPENAI_API_KEY"] = value
                parsed = ai_parser.parse_customer_request(self.text)
                self.assert_demo(parsed)
        self.sdk.assert_not_called()

    def test_api_failures_do_not_expose_raw_error_details(self):
        sensitive_detail = "offline-sensitive-detail-must-not-be-shown"
        request = SimpleNamespace(method="POST", url="https://example.invalid/responses")
        errors = [
            AuthenticationError(
                sensitive_detail,
                response=SimpleNamespace(status_code=401, request=request, headers={}),
                body={"error": sensitive_detail},
            ),
            RateLimitError(
                sensitive_detail,
                response=SimpleNamespace(status_code=429, request=request, headers={}),
                body={"error": sensitive_detail},
            ),
            APITimeoutError(request=request),
            APIConnectionError(message=sensitive_detail, request=request),
            APIStatusError(
                sensitive_detail,
                response=SimpleNamespace(status_code=500, request=request, headers={}),
                body={"error": sensitive_detail},
            ),
            APIError(sensitive_detail, request=request, body={"error": sensitive_detail}),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.client.responses.create.side_effect = error
                parsed = self.assert_fallback(
                    "timeout" if isinstance(error, APITimeoutError) else "api_error"
                )
                self.assertEqual(parsed.token_used, 0)
                self.assertNotIn(sensitive_detail, parsed.error)
        self.assertNotIn(sensitive_detail, self.log_path.read_text(encoding="utf-8"))

    def test_malformed_or_empty_json_is_rejected(self):
        for output in ("", "not-json", "{", '```json\n{}\n```'):
            with self.subTest(output=output):
                self.client.responses.create.return_value.output_text = output
                self.assert_invalid_response_is_rejected("json_error")

    def test_non_object_json_is_rejected(self):
        for fields in ([], None, 123, "text"):
            with self.subTest(fields=fields):
                self.set_response(fields)
                self.assert_invalid_response_is_rejected()

    def test_each_missing_field_is_rejected_instead_of_filled_in(self):
        for missing_field in self.fields:
            with self.subTest(missing_field=missing_field):
                fields = self.fields.copy()
                del fields[missing_field]
                self.set_response(fields)
                self.assert_invalid_response_is_rejected()

    def test_unexpected_field_is_rejected(self):
        self.set_response({**self.fields, "status": "已完成"})
        self.assert_invalid_response_is_rejected()

    def test_non_object_field_is_rejected(self):
        for value in (None, "演示电话001", 123, [], False):
            with self.subTest(value=value):
                self.set_response({**self.fields, "phone": value})
                self.assert_invalid_response_is_rejected()

    def test_nested_missing_or_extra_keys_are_rejected(self):
        for item in (
            {},
            {"value": "演示电话001"},
            {"confidence": 0.95},
            {"value": "演示电话001", "confidence": 0.95, "explanation": "extra"},
        ):
            with self.subTest(item=item):
                self.set_response({**self.fields, "phone": item})
                self.assert_invalid_response_is_rejected()

    def test_non_string_values_are_rejected(self):
        for value in (None, 12345, False, [], {}):
            with self.subTest(value=value):
                self.set_response(
                    {**self.fields, "phone": {"value": value, "confidence": 0.95}}
                )
                self.assert_invalid_response_is_rejected()

    def test_invalid_confidence_is_rejected_without_exposing_returned_data(self):
        sensitive_detail = "offline-sensitive-model-content"
        for confidence in (
            None, "0.9", sensitive_detail, False, True, [], {},
            -0.01, 1.01, float("nan"), float("inf"), float("-inf"), 10**1000,
        ):
            with self.subTest(confidence=confidence):
                self.set_response(
                    {**self.fields, "phone": {"value": sensitive_detail, "confidence": confidence}}
                )
                parsed = self.assert_invalid_response_is_rejected()
                self.assertNotIn(sensitive_detail, parsed.error)
        self.assertNotIn(sensitive_detail, self.log_path.read_text(encoding="utf-8"))

    def test_refusal_is_rejected_even_with_valid_json(self):
        refusal = "offline-refusal-detail-must-not-be-shown"
        self.set_response(
            self.fields,
            output=[
                SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(type="refusal", refusal=refusal)],
                )
            ],
        )
        parsed = self.assert_fallback()
        self.assertNotIn(refusal, parsed.error)
        self.assertNotIn(refusal, self.log_path.read_text(encoding="utf-8"))

    def test_non_completed_response_is_rejected_even_with_valid_json(self):
        for status in ("incomplete", "failed", "in_progress", None):
            with self.subTest(status=status):
                self.set_response(self.fields, status=status)
                self.assert_fallback()

    def test_parsed_fields_flow_into_ticket_and_existing_rules(self):
        for missing_field, expected_status in (
            (None, "待确认"),
            ("phone", "人工审核"),
            ("address", "人工审核"),
        ):
            with self.subTest(missing_field=missing_field):
                fields = self.fields.copy()
                if missing_field:
                    fields[missing_field] = {"value": "", "confidence": 0}
                self.set_response(fields)
                parsed = ai_parser.parse_customer_request(self.text)
                ticket = create_ticket(self.text, parsed)
                self.assertEqual(ticket["customer_input"], self.text)
                self.assertTrue(ticket["id"])
                self.assertEqual(ticket["status"], expected_status)
                for field, item in parsed.items():
                    self.assertEqual(ticket[field], item["value"])
                self.assertEqual(json.loads(ticket["confidence_score"]), parsed)
                self.assertIs(ticket["review_required"], missing_field is not None)
                self.save_ticket.assert_called_with(ticket)

    def test_success_metadata_stays_outside_json_and_logs_once(self):
        self.client.responses.create.return_value.usage = SimpleNamespace(total_tokens=321)
        with patch.object(ai_parser.time, "perf_counter", side_effect=[10.0, 10.125]):
            parsed = ai_parser.parse_customer_request(self.text)

        self.assertEqual(parsed, self.fields)
        self.assertEqual(json.loads(json.dumps(parsed)), self.fields)
        self.assertEqual(parsed.token_used, 321)
        self.assertEqual(parsed.response_time, 0.125)
        self.assertEqual(parsed.call_status, "success")
        self.assertEqual(parsed.error, "")
        self.assertTrue(parsed.api_called)
        self.assertEqual(parsed.logging_error, "")
        self.client.responses.create.assert_called_once()
        entries = json.loads(self.log_path.read_text(encoding="utf-8"))
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(
            set(entry),
            {
                "timestamp", "input", "output", "status", "error", "token_used",
                "response_time", "api_called", "request_time", "response_timestamp", "model",
            },
        )
        self.assertEqual(datetime.fromisoformat(entry["timestamp"]).utcoffset(), timedelta(hours=8))
        self.assertEqual(entry["input"], self.text)
        self.assertEqual(entry["output"], self.fields)
        self.assertEqual(entry["status"], "success")
        self.assertEqual(entry["error"], "")
        self.assertEqual(entry["token_used"], 321)
        self.assertEqual(entry["response_time"], 0.125)
        self.assertTrue(entry["api_called"])
        self.assertEqual(entry["request_time"], parsed.request_time)
        self.assertEqual(entry["response_timestamp"], parsed.response_timestamp)
        self.assertEqual(entry["model"], "gpt-4o-mini")
        self.assertEqual(datetime.fromisoformat(parsed.request_time).utcoffset(), timedelta(hours=8))
        self.assertEqual(datetime.fromisoformat(parsed.response_timestamp).utcoffset(), timedelta(hours=8))
        self.assertNotIn("offline-test-key", self.log_path.read_text(encoding="utf-8"))

    def test_invalid_json_preserves_known_usage_and_timing_without_logging_raw_output(self):
        raw_output = "offline-private-invalid-response"
        response = self.client.responses.create.return_value
        response.output_text = raw_output
        response.usage = SimpleNamespace(total_tokens=77)
        with patch.object(ai_parser.time, "perf_counter", side_effect=[100.0, 101.5]):
            parsed = self.assert_fallback("json_error")

        self.assertEqual(parsed.token_used, 77)
        self.assertEqual(parsed.response_time, 1.5)
        entries = json.loads(self.log_path.read_text(encoding="utf-8"))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["output"], dict(parsed))
        self.assertEqual(entries[0]["token_used"], 77)
        self.assertEqual(entries[0]["response_time"], 1.5)
        self.assertEqual(entries[0]["status"], "json_error")
        self.assertIsNotNone(parsed.request_time)
        self.assertIsNotNone(parsed.response_timestamp)
        self.assertEqual(entries[0]["response_timestamp"], parsed.response_timestamp)
        self.assertNotIn(raw_output, self.log_path.read_text(encoding="utf-8"))

    def test_usage_accepts_only_non_negative_integers(self):
        for usage, expected in (
            (None, 0),
            (SimpleNamespace(), 0),
            (SimpleNamespace(total_tokens=None), 0),
            (SimpleNamespace(total_tokens=-1), 0),
            (SimpleNamespace(total_tokens=True), 0),
            (SimpleNamespace(total_tokens=1.5), 0),
            (SimpleNamespace(total_tokens="123"), 0),
            (SimpleNamespace(total_tokens=0), 0),
            (SimpleNamespace(total_tokens=123), 123),
        ):
            with self.subTest(usage=usage):
                self.client.responses.create.return_value.usage = usage
                parsed = ai_parser.parse_customer_request(self.text)
                self.assertEqual(parsed.token_used, expected)
                self.assertIs(type(parsed.token_used), int)

    def test_unexpected_configuration_or_sdk_errors_return_safe_fallbacks(self):
        sensitive_detail = "offline-unexpected-private-error"
        for target, api_called in (
            (self.load_dotenv, False),
            (self.sdk, False),
            (self.client.responses.create, True),
        ):
            with self.subTest(api_called=api_called):
                target.side_effect = RuntimeError(sensitive_detail)
                parsed = self.assert_fallback("unexpected_error", api_called=api_called)
                self.assertEqual(parsed.token_used, 0)
                self.assertNotIn(sensitive_detail, parsed.error)
                target.side_effect = None
        self.assertEqual(self.client.responses.create.call_count, 1)
        entries = json.loads(self.log_path.read_text(encoding="utf-8"))
        self.assertEqual(len(entries), 3)
        self.assertNotIn(sensitive_detail, self.log_path.read_text(encoding="utf-8"))

    def test_logging_errors_preserve_success_and_never_retry_api(self):
        sensitive_detail = "offline-private-log-error"
        for error in (OSError(sensitive_detail), ValueError(sensitive_detail), RuntimeError(sensitive_detail)):
            with self.subTest(error=type(error).__name__):
                self.client.responses.create.reset_mock()
                with patch.object(ai_parser, "append_ai_call", side_effect=error) as append:
                    parsed = ai_parser.parse_customer_request(self.text)
                self.assertEqual(parsed, self.fields)
                self.assertEqual(parsed.call_status, "success")
                self.assertTrue(parsed.logging_error)
                self.assertNotIn(sensitive_detail, parsed.logging_error)
                append.assert_called_once()
                self.client.responses.create.assert_called_once()

    def test_failed_api_still_produces_a_ticket_for_manual_review(self):
        request = SimpleNamespace(method="POST", url="https://example.invalid/responses")
        self.client.responses.create.side_effect = APITimeoutError(request=request)
        with patch.object(ai_parser.time, "perf_counter", side_effect=[20.0, 50.0]):
            parsed = self.assert_fallback("timeout")
        ticket = create_ticket(self.text, parsed)
        self.assertEqual(ticket["customer_input"], self.text)
        self.assertEqual(ticket["status"], "人工审核")
        self.assertTrue(ticket["review_required"])
        self.assertEqual(ticket["token_used"], 0)
        self.assertEqual(ticket["response_time"], 30.0)
        self.assertIsNotNone(parsed.request_time)
        self.assertIsNone(parsed.response_timestamp)
        self.assertEqual(parsed.model, "gpt-4o-mini")
        self.assertEqual(json.loads(ticket["confidence_score"]), parsed)
        self.save_ticket.assert_called_once_with(ticket)

    def test_missing_key_logs_configuration_failure_without_counting_api(self):
        # 保留既有回归名称；本阶段缺少 Key 时已改为自动演示。
        os.environ.pop("OPENAI_API_KEY")
        parsed = ai_parser.parse_customer_request(self.text)
        self.assert_demo(parsed)
        self.sdk.assert_not_called()
        entries = json.loads(self.log_path.read_text(encoding="utf-8"))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["output"], parsed)
        self.assertEqual(entries[0]["status"], "demo")
        self.assertIsNone(entries[0]["response_time"])
        self.assertEqual(entries[0]["token_used"], 0)
        self.assertFalse(entries[0]["api_called"])
        self.assertEqual(entries[0]["model"], "demo-parser")
        self.assertIsNone(entries[0]["request_time"])
        self.assertIsNone(entries[0]["response_timestamp"])

    def test_explicit_demo_with_key_uses_only_fixed_sample_without_sdk(self):
        config.DEMO_MODE = True
        parsed = ai_parser.parse_customer_request(config.DEMO_CUSTOMER_REQUEST)
        expected = {field: {"value": "", "confidence": 0.0} for field in self.fields}
        expected.update({
            "service_type": {"value": "上门护理", "confidence": 0.95},
            "appointment_time": {"value": "明天下午两点", "confidence": 0.95},
            "address": {"value": "示例区演示小区", "confidence": 0.95},
        })
        self.assert_demo(parsed, expected)
        self.sdk.assert_not_called()
        entries = json.loads(self.log_path.read_text(encoding="utf-8"))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["input"], config.DEMO_CUSTOMER_REQUEST)
        self.assertEqual(entries[0]["output"], expected)
        self.assertEqual(ai_logging.get_ai_call_stats()["ai_call_count"], 0)
        self.assertIsNone(ai_logging.get_ai_call_stats()["avg_response_time"])
        ticket = create_ticket(config.DEMO_CUSTOMER_REQUEST, parsed)
        self.assertEqual(ticket["status"], "人工审核")
        self.assertTrue(ticket["review_required"])
        self.assertEqual(ticket["phone"], "")

    def test_demo_unknown_or_modified_sample_never_invents_fields(self):
        config.DEMO_MODE = True
        for text in (
            self.text,
            "完全不同的客户需求",
            config.DEMO_CUSTOMER_REQUEST + "请尽快。",
            config.DEMO_CUSTOMER_REQUEST.replace("示例区演示小区", "其他地址"),
        ):
            with self.subTest(text=text):
                self.assert_demo(ai_parser.parse_customer_request(text))
        self.sdk.assert_not_called()

    def test_missing_key_automatically_enables_the_same_sample_demo(self):
        os.environ.pop("OPENAI_API_KEY")
        parsed = ai_parser.parse_customer_request(config.DEMO_CUSTOMER_REQUEST)
        self.assertEqual(parsed.call_status, "demo")
        self.assertEqual(parsed["service_type"], {"value": "上门护理", "confidence": 0.95})
        self.assertEqual(parsed["phone"], {"value": "", "confidence": 0.0})
        self.assertFalse(parsed.api_called)
        self.sdk.assert_not_called()

    def test_startup_status_reports_effective_mode_without_sdk_or_key_disclosure(self):
        os.environ["OPENAI_MODEL"] = "offline-configured-model"
        for demo_mode, key, expected_demo in (
            (True, "offline-test-key", True),
            (False, "offline-test-key", False),
            (False, "", True),
        ):
            with self.subTest(demo_mode=demo_mode, key_present=bool(key)):
                config.DEMO_MODE = demo_mode
                os.environ["OPENAI_API_KEY"] = key
                status = ai_parser.get_ai_status()
                self.assertEqual(
                    set(status),
                    {"demo_mode", "api_key_configured", "model", "configuration_error"},
                )
                self.assertIs(status["demo_mode"], expected_demo)
                self.assertIs(status["api_key_configured"], bool(key))
                self.assertEqual(status["model"], "demo-parser" if expected_demo else "offline-configured-model")
                self.assertEqual(status["configuration_error"], "")
                self.assertNotIn("offline-test-key", json.dumps(status))
        self.sdk.assert_not_called()
        self.assertFalse(self.log_path.exists())

    def test_startup_configuration_error_is_safe_and_does_not_call_sdk(self):
        private_error = "offline-private-configuration-error"
        self.load_dotenv.side_effect = RuntimeError(private_error)
        status = ai_parser.get_ai_status()
        self.assertTrue(status["configuration_error"])
        self.assertNotIn(private_error, json.dumps(status))
        self.assertFalse(status["api_key_configured"])
        self.sdk.assert_not_called()
        self.assertFalse(self.log_path.exists())
        self.assert_fallback("unexpected_error", api_called=False)

    def test_real_response_model_and_event_times_are_logged(self):
        os.environ["OPENAI_MODEL"] = "configured-model-alias"
        self.client.responses.create.return_value.model = "actual-model-snapshot"
        request_time = "2026-10-12T09:00:00+08:00"
        response_time = "2026-10-12T09:00:01+08:00"
        logged_time = "2026-10-12T09:00:02+08:00"
        with patch.object(ai_parser, "_timestamp", side_effect=[request_time, response_time, logged_time]):
            parsed = ai_parser.parse_customer_request(self.text)
        self.assertEqual(parsed.model, "actual-model-snapshot")
        self.assertEqual(parsed.request_time, request_time)
        self.assertEqual(parsed.response_timestamp, response_time)
        self.assertEqual(self.client.responses.create.call_args.kwargs["model"], "configured-model-alias")
        entry = json.loads(self.log_path.read_text(encoding="utf-8"))[0]
        self.assertEqual(entry["timestamp"], logged_time)
        self.assertEqual(entry["request_time"], request_time)
        self.assertEqual(entry["response_timestamp"], response_time)
        self.assertEqual(entry["model"], "actual-model-snapshot")
        self.assertEqual(set(parsed), set(self.fields))

    def test_demo_helper_bypasses_real_mode_configuration_and_sdk(self):
        self.assertFalse(config.DEMO_MODE)
        expected = {field: {"value": "", "confidence": 0.0} for field in self.fields}
        expected.update({
            "service_type": {"value": "上门护理", "confidence": 0.95},
            "appointment_time": {"value": "明天下午两点", "confidence": 0.95},
            "address": {"value": "示例区演示小区", "confidence": 0.95},
        })
        with (
            patch.object(ai_parser, "_load_configuration", side_effect=AssertionError("must not read config")),
            patch.object(ai_parser, "append_ai_call", wraps=ai_parser.append_ai_call) as append,
        ):
            parsed = ai_parser.parse_demo_request(config.DEMO_CUSTOMER_REQUEST)
        self.assert_demo(parsed, expected)
        self.sdk.assert_not_called()
        self.load_dotenv.assert_not_called()
        append.assert_called_once()
        entries = json.loads(self.log_path.read_text(encoding="utf-8"))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["output"], expected)
        self.assertEqual(entries[0]["status"], "demo")
        self.assertFalse(entries[0]["api_called"])

    def test_demo_helper_logging_failure_preserves_parsed_fields(self):
        private_error = "offline-private-demo-log-error"
        with patch.object(ai_parser, "append_ai_call", side_effect=OSError(private_error)) as append:
            parsed = ai_parser.parse_demo_request(config.DEMO_CUSTOMER_REQUEST)
        self.assertEqual(parsed["service_type"], {"value": "上门护理", "confidence": 0.95})
        self.assertEqual(parsed["appointment_time"], {"value": "明天下午两点", "confidence": 0.95})
        self.assertEqual(parsed["address"], {"value": "示例区演示小区", "confidence": 0.95})
        self.assertEqual(parsed.call_status, "demo")
        self.assertTrue(parsed.logging_error)
        self.assertNotIn(private_error, parsed.logging_error)
        self.assertEqual(parsed.error, "")
        self.assertFalse(parsed.api_called)
        append.assert_called_once()
        self.sdk.assert_not_called()
        self.load_dotenv.assert_not_called()

    def test_demo_helper_invalid_input_has_no_side_effects(self):
        with patch.object(ai_parser, "append_ai_call") as append:
            for text in ("", " \t\n ", None, 123):
                with self.subTest(text=text), self.assertRaises(ValueError):
                    ai_parser.parse_demo_request(text)
        append.assert_not_called()
        self.sdk.assert_not_called()
        self.load_dotenv.assert_not_called()
        self.assertFalse(self.log_path.exists())


if __name__ == "__main__":
    unittest.main()
