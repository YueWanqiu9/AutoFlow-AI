"""使用 OpenAI 解析客户需求，对外保持统一的六字段字典接口。"""

import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from openai import APIError, APITimeoutError, AuthenticationError, OpenAI, RateLimitError

import config
from modules.ai_logging import append_ai_call
from modules.prompts import SYSTEM_PROMPT

REQUEST_FIELDS = (
    "service_type",
    "appointment_time",
    "address",
    "customer_name",
    "phone",
    "urgency",
)


REQUEST_SCHEMA = {
    "type": "object",
    "properties": {
        field: {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["value", "confidence"],
            "additionalProperties": False,
        }
        for field in REQUEST_FIELDS
    },
    "required": list(REQUEST_FIELDS),
    "additionalProperties": False,
}

class AIParserError(ValueError):
    """可以向用户展示的解析错误，不包含密钥或原始 API 响应。"""

    def __init__(self, message: str, call_status: str = "invalid_response"):
        super().__init__(message)
        self.call_status = call_status


class ParsedRequest(dict):
    """六字段字典；调用元数据放在属性中，避免混入解析 JSON。"""

    def __init__(self, fields: dict | None = None):
        super().__init__(
            {field: {"value": "", "confidence": 0.0} for field in REQUEST_FIELDS}
            if fields is None else fields
        )
        self.token_used: int = 0
        self.response_time: float | None = None
        self.call_status: str = "success"
        self.error: str = ""
        self.api_called: bool = False
        self.logging_error: str = ""
        self.request_time: str | None = None
        self.response_timestamp: str | None = None
        self.model: str = ""


def _load_configuration() -> tuple[str, str]:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("OPENAI_MODEL", "").strip() or "gpt-4o-mini"
    return api_key, model


def get_ai_status() -> dict:
    """仅检查本地配置供页面显示；不返回密钥，也不调用或验证远端 API。"""
    try:
        api_key, model = _load_configuration()
    except Exception:
        return {
            "demo_mode": True,
            "api_key_configured": False,
            "model": "demo-parser",
            "configuration_error": "AI 配置读取失败，请检查项目 .env 与运行环境。",
        }
    demo_mode = bool(config.DEMO_MODE) or not api_key
    return {
        "demo_mode": bool(demo_mode),
        "api_key_configured": bool(api_key),
        "model": "demo-parser" if demo_mode else model,
        "configuration_error": "",
    }


def _timestamp() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def _demo_parse(text: str) -> dict:
    """仅识别固定演示样本，其他输入留空交由人工处理。"""
    fields = {field: {"value": "", "confidence": 0.0} for field in REQUEST_FIELDS}
    if text == config.DEMO_CUSTOMER_REQUEST:
        for field, value in {
            "service_type": "上门护理",
            "appointment_time": "明天下午两点",
            "address": "示例区演示小区",
        }.items():
            fields[field] = {"value": value, "confidence": 0.95}
    return fields


def parse_demo_request(text: str) -> ParsedRequest:
    """独立演示入口：始终本地解析，不读取 API 配置或调用 SDK。"""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("请先输入客户需求，再创建工单。")
    result = ParsedRequest(_demo_parse(text))
    result.call_status = "demo"
    result.model = "demo-parser"
    _record_result(text, result)
    return result


def parse_customer_request(text: str) -> ParsedRequest:
    """返回值和自评分；AI 失败时返回空字段供人工处理，并记录一次调用日志。"""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("请先输入客户需求，再创建工单。")

    result = ParsedRequest()

    try:
        api_key, model = _load_configuration()
        if config.DEMO_MODE or not api_key:
            result.update(_demo_parse(text))
            result.call_status = "demo"
            result.model = "demo-parser"
        else:
            result.model = model
            with OpenAI(api_key=api_key, timeout=30.0, max_retries=0) as client:
                result.request_time = _timestamp()
                result.api_called = True
                started = time.perf_counter()
                try:
                    response = client.responses.create(
                        model=model,
                        input=[
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": text},
                        ],
                        text={
                            "format": {
                                "type": "json_schema",
                                "name": "customer_request",
                                "schema": REQUEST_SCHEMA,
                                "strict": True,
                            }
                        },
                        max_output_tokens=1000,
                        store=False,
                    )
                finally:
                    result.response_time = max(0.0, time.perf_counter() - started)

                result.response_timestamp = _timestamp()
                response_model = getattr(response, "model", None)
                if isinstance(response_model, str) and response_model.strip():
                    result.model = response_model.strip()
                token_used = getattr(getattr(response, "usage", None), "total_tokens", 0)
                if type(token_used) is int and token_used >= 0:
                    result.token_used = token_used
            if response.status != "completed":
                raise AIParserError("AI 未返回完整结果，已保留原始需求供人工处理。")
            for item in response.output:
                if item.type == "message" and any(part.type == "refusal" for part in item.content):
                    raise AIParserError("AI 拒绝解析本次需求，已保留原始需求供人工处理。")
            result.update(_parse_json(response.output_text))
    except AuthenticationError:
        result.call_status = "api_error"
        result.error = "OpenAI 身份验证失败，请检查 API Key 配置。"
    except RateLimitError:
        result.call_status = "api_error"
        result.error = "OpenAI 请求受限，请检查额度或稍后重试。"
    except APITimeoutError:
        result.call_status = "timeout"
        result.error = "AI 请求超时，已保留原始需求供人工处理。"
    except APIError:
        result.call_status = "api_error"
        result.error = "OpenAI API 调用失败，请检查网络、模型配置或稍后重试。"
    except AIParserError as error:
        result.call_status = error.call_status
        result.error = str(error)
    except Exception:
        result.call_status = "unexpected_error"
        result.error = "AI 解析发生异常，已保留原始需求供人工处理。"

    _record_result(text, result)
    return result


def _record_result(text: str, result: ParsedRequest) -> None:
    """记录一次解析结果；日志错误只附加提示，不影响业务结果。"""
    try:
        entry = {
            "timestamp": _timestamp(),
            "input": text,
            "output": dict(result),
            "status": result.call_status,
            "error": result.error,
            "token_used": result.token_used,
            "response_time": result.response_time,
            "api_called": result.api_called,
            "request_time": result.request_time,
            "response_timestamp": result.response_timestamp,
            "model": result.model,
        }
        append_ai_call(entry)
    except Exception:
        # 日志是辅助记录；任何写入错误都不应丢失解析结果或触发再次调用。
        result.logging_error = "AI 调用日志写入失败，解析结果仍保留；请检查日志目录权限。"


def _parse_json(content: str) -> dict[str, dict[str, str | float]]:
    """严格校验字段结构和置信度；空值的置信度统一为零。"""
    try:
        result = json.loads(content)
    except (ValueError, TypeError):
        raise AIParserError("AI 返回的内容无法解析为 JSON，请重试。", "json_error") from None

    if not isinstance(result, dict):
        raise AIParserError("AI 返回的 JSON 必须是对象，请重试。")
    missing = [field for field in REQUEST_FIELDS if field not in result]
    if missing:
        raise AIParserError(f"AI 返回缺少必填字段：{', '.join(missing)}。请重试。")
    if set(result) != set(REQUEST_FIELDS):
        raise AIParserError("AI 返回了未定义的字段，请重试。")
    normalized = {}
    for field in REQUEST_FIELDS:
        item = result[field]
        if not isinstance(item, dict) or set(item) != {"value", "confidence"}:
            raise AIParserError(f"AI 返回的字段 {field} 必须只包含 value 和 confidence。请重试。")
        if not isinstance(item["value"], str):
            raise AIParserError(f"AI 返回的字段 {field} 的 value 必须为字符串，请重试。")
        confidence = item["confidence"]
        if (
            type(confidence) not in (int, float)
            or not 0 <= confidence <= 1
            or not math.isfinite(confidence)
        ):
            raise AIParserError(f"AI 返回的字段 {field} 的 confidence 必须为 0 到 1 的有限数字。请重试。")
        value = item["value"].strip()
        normalized[field] = {"value": value, "confidence": float(confidence) if value else 0.0}
    return normalized
