"""运营辅助任务的结构化调用；优先级、建议、补全、推荐解释分别记录日志。"""

import json
import math
import os
import time
from datetime import datetime, timedelta, timezone

from openai import APIError, APITimeoutError, OpenAI

from modules.ai_parser import get_ai_status
from modules.ai_logging import append_ai_call
from modules.prompts import ADVISOR_PROMPT, CONVERSATION_PROMPT, PRIORITY_PROMPT, RECOMMENDATION_PROMPT
from modules.rules import COMPLETION_FIELDS


def validate_recommendation_content(output: dict, context: dict) -> dict:
    """模型只能解释固定对象和本地提供的事实；不接受自由增补或评分字段。"""
    if not isinstance(output, dict) or set(output) != {"recommended_staff_id", "reason", "risk_notes"}:
        raise ValueError("推荐解释字段不正确。")
    if type(output["recommended_staff_id"]) is not int or output["recommended_staff_id"] != context["fixed_staff_id"]:
        raise ValueError("推荐解释不能更换规则确定的人员。")
    result = {"recommended_staff_id": output["recommended_staff_id"]}
    for field, allowed_key in (("reason", "allowed_reasons"), ("risk_notes", "allowed_risk_notes")):
        items = output[field]
        if (not isinstance(items, list) or len(items) > 16
                or any(not isinstance(item, str) or item.strip() not in context[allowed_key] for item in items)):
            raise ValueError("推荐说明包含未经确认的内容。")
        result[field] = list(dict.fromkeys(item.strip() for item in items))
    if not result["reason"]:
        raise ValueError("推荐解释必须包含理由。")
    return result


def request_recommendation(context: dict, ticket_id: str | None = None) -> dict | None:
    """独立 recommendation 任务；返回经本地校验的说明，失败返回 None。"""
    entry = {
        "timestamp": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "task_type": "recommendation", "ticket_id": ticket_id, "input": context,
        "output": {}, "status": "configuration_error", "error": "",
        "api_called": False, "token_used": 0, "response_time": None, "model": "",
    }
    result = None
    try:
        configuration = get_ai_status()
        if configuration["demo_mode"]:
            entry["error"] = "AI不可用，使用规则推荐说明。"
            return None
        entry["model"] = configuration["model"]
        schema = {
            "type": "object",
            "properties": {
                "recommended_staff_id": {"type": "integer", "enum": [context["fixed_staff_id"]]},
                "reason": {"type": "array", "items": {"type": "string", "enum": context["allowed_reasons"]}},
                "risk_notes": {"type": "array", "items": {"type": "string", "enum": context["allowed_risk_notes"]}},
            },
            "required": ["recommended_staff_id", "reason", "risk_notes"], "additionalProperties": False,
        }
        with OpenAI(api_key=os.getenv("OPENAI_API_KEY", "").strip(), timeout=15.0, max_retries=0) as client:
            entry["api_called"] = True
            started = time.perf_counter()
            try:
                response = client.responses.create(
                    model=entry["model"],
                    input=[{"role": "system", "content": RECOMMENDATION_PROMPT},
                           {"role": "user", "content": json.dumps(context, ensure_ascii=False, allow_nan=False)}],
                    text={"format": {"type": "json_schema", "name": "staff_recommendation", "schema": schema, "strict": True}},
                    max_output_tokens=1200, store=False,
                )
            finally:
                duration = time.perf_counter() - started
                entry["response_time"] = max(0.0, duration) if math.isfinite(duration) else None
        tokens = getattr(getattr(response, "usage", None), "total_tokens", 0)
        if type(tokens) is int and tokens >= 0:
            entry["token_used"] = tokens
        entry["model"] = getattr(response, "model", None) or entry["model"]
        if response.status != "completed":
            raise ValueError("模型输出未完成")
        result = validate_recommendation_content(json.loads(response.output_text), context)
        entry.update(output=result, status="success")
    except APITimeoutError:
        entry.update(status="timeout", error="推荐解释请求超时，使用规则说明。")
    except APIError:
        entry.update(status="api_error", error="推荐解释调用失败，使用规则说明。")
    except (ValueError, TypeError, KeyError):
        entry.update(status="invalid_response", error="推荐解释格式或事实校验失败，使用规则说明。")
    except Exception:
        entry.update(status="unexpected_error", error="AI不可用，使用规则推荐说明。")
    finally:
        try:
            append_ai_call(entry)
        except Exception:
            # 日志写入失败不影响已有规则候选，也不重试模型请求。
            pass
    return result


def request_conversation(context: dict, ticket_id: str) -> dict | None:
    """回复提取使用独立任务日志，不调用原六字段解析器。"""
    entry = {
        "timestamp": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "task_type": "conversation", "ticket_id": ticket_id, "input": context,
        "output": {}, "status": "configuration_error", "error": "",
        "api_called": False, "token_used": 0, "response_time": None, "model": "",
    }
    result = None
    try:
        configuration = get_ai_status()
        if configuration["demo_mode"]:
            entry["error"] = "AI不可用，使用规则补全。"
            return None
        entry["model"] = configuration["model"]
        schema = {
            "type": "object", "properties": {field: {"type": "string"} for field in COMPLETION_FIELDS},
            "required": list(COMPLETION_FIELDS), "additionalProperties": False,
        }
        with OpenAI(api_key=os.getenv("OPENAI_API_KEY", "").strip(), timeout=15.0, max_retries=0) as client:
            entry["api_called"] = True
            started = time.perf_counter()
            try:
                response = client.responses.create(
                    model=entry["model"],
                    input=[{"role": "system", "content": CONVERSATION_PROMPT},
                           {"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
                    text={"format": {"type": "json_schema", "name": "conversation_fields", "schema": schema, "strict": True}},
                    max_output_tokens=1000, store=False,
                )
            finally:
                duration = time.perf_counter() - started
                entry["response_time"] = max(0.0, duration) if math.isfinite(duration) else None
        tokens = getattr(getattr(response, "usage", None), "total_tokens", 0)
        if type(tokens) is int and tokens >= 0:
            entry["token_used"] = tokens
        entry["model"] = getattr(response, "model", None) or entry["model"]
        if response.status != "completed":
            raise ValueError("模型输出未完成")
        output = json.loads(response.output_text)
        if (not isinstance(output, dict) or set(output) != set(COMPLETION_FIELDS)
                or any(not isinstance(value, str) or len(value) > 500 for value in output.values())):
            raise ValueError("回复提取格式不正确")
        result = output
        entry.update(output=output, status="success")
    except APITimeoutError:
        entry.update(status="timeout", error="回复解析超时，使用规则补全。")
    except APIError:
        entry.update(status="api_error", error="回复解析失败，使用规则补全。")
    except (ValueError, TypeError):
        entry.update(status="invalid_response", error="回复解析格式无效，使用规则补全。")
    except Exception:
        entry.update(status="unexpected_error", error="AI不可用，使用规则补全。")
    finally:
        try:
            append_ai_call(entry)
        except Exception:
            pass
    return result


def validate_advice_content(output: dict) -> dict:
    fields = ("missing_information", "next_steps", "attention_points")
    if not isinstance(output, dict) or set(output) != set(fields):
        raise ValueError("处理建议字段不完整。")
    for field in fields:
        items = output[field]
        if (not isinstance(items, list) or len(items) > 12
                or any(not isinstance(item, str) or not 1 <= len(item.strip()) <= 600 for item in items)):
            raise ValueError("处理建议必须为有限长度的文本列表。")
    if not output["next_steps"]:
        raise ValueError("处理建议必须包含处理步骤。")
    return {field: [item.strip() for item in output[field]] for field in fields}


def request_advice(context: dict, ticket_id: str | None = None) -> dict | None:
    """复用现有 SDK 配置，advisor 日志不进入解析或优先级统计。"""
    entry = {
        "timestamp": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "task_type": "advisor", "ticket_id": ticket_id, "input": context,
        "output": {}, "status": "configuration_error", "error": "",
        "api_called": False, "token_used": 0, "response_time": None, "model": "",
    }
    result = None
    try:
        configuration = get_ai_status()
        if configuration["demo_mode"]:
            entry["error"] = "AI不可用，使用规则建议。"
            return None
        entry["model"] = configuration["model"]
        fields = ("missing_information", "next_steps", "attention_points")
        schema = {
            "type": "object", "properties": {field: {"type": "array", "items": {"type": "string"}} for field in fields},
            "required": list(fields), "additionalProperties": False,
        }
        with OpenAI(api_key=os.getenv("OPENAI_API_KEY", "").strip(), timeout=15.0, max_retries=0) as client:
            entry["api_called"] = True
            started = time.perf_counter()
            try:
                response = client.responses.create(
                    model=entry["model"],
                    input=[{"role": "system", "content": ADVISOR_PROMPT},
                           {"role": "user", "content": json.dumps(context, ensure_ascii=False, allow_nan=False)}],
                    text={"format": {"type": "json_schema", "name": "ticket_advice", "schema": schema, "strict": True}},
                    max_output_tokens=1500, store=False,
                )
            finally:
                duration = time.perf_counter() - started
                entry["response_time"] = max(0.0, duration) if math.isfinite(duration) else None
        entry["model"] = getattr(response, "model", None) or entry["model"]
        tokens = getattr(getattr(response, "usage", None), "total_tokens", 0)
        if type(tokens) is int and tokens >= 0:
            entry["token_used"] = tokens
        if response.status != "completed":
            raise ValueError("未完成的模型输出")
        result = validate_advice_content(json.loads(response.output_text))
        entry.update(output=result, status="success")
    except APITimeoutError:
        entry.update(status="timeout", error="处理建议请求超时，使用规则建议。")
    except APIError:
        entry.update(status="api_error", error="处理建议调用失败，使用规则建议。")
    except (ValueError, TypeError):
        entry.update(status="invalid_response", error="处理建议格式无效，使用规则建议。")
    except Exception:
        entry.update(status="unexpected_error", error="AI不可用，使用规则建议。")
    finally:
        try:
            append_ai_call(entry)
        except Exception:
            pass
    return result


def explain_priority(rule_result: dict, ticket_id: str | None = None) -> str | None:
    """只发送规则依据，不发送客户姓名、电话、地址或完整原文。"""
    entry = {
        "timestamp": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "task_type": "priority", "ticket_id": ticket_id,
        "input": {"level": rule_result["level"], "reason": rule_result["reason"]},
        "output": {}, "status": "configuration_error", "error": "",
        "api_called": False, "token_used": 0, "response_time": None, "model": "",
    }
    explanation = None
    try:
        configuration = get_ai_status()
        if configuration["demo_mode"]:
            return None
        entry["model"] = configuration["model"]
        schema = {
            "type": "object",
            "properties": {"level": {"type": "string", "enum": [rule_result["level"]]}, "reason": {"type": "string"}},
            "required": ["level", "reason"], "additionalProperties": False,
        }
        with OpenAI(api_key=os.getenv("OPENAI_API_KEY", "").strip(), timeout=15.0, max_retries=0) as client:
            entry["api_called"] = True
            started = time.perf_counter()
            try:
                response = client.responses.create(
                    model=entry["model"],
                    input=[{"role": "system", "content": PRIORITY_PROMPT},
                           {"role": "user", "content": json.dumps(entry["input"], ensure_ascii=False)}],
                    text={"format": {"type": "json_schema", "name": "priority_explanation", "schema": schema, "strict": True}},
                    max_output_tokens=500, store=False,
                )
            finally:
                duration = time.perf_counter() - started
                entry["response_time"] = max(0.0, duration) if math.isfinite(duration) else None
        token_used = getattr(getattr(response, "usage", None), "total_tokens", 0)
        if type(token_used) is int and token_used >= 0:
            entry["token_used"] = token_used
        if response.status != "completed":
            raise ValueError("未完成的模型输出")
        output = json.loads(response.output_text)
        if (not isinstance(output, dict) or set(output) != {"level", "reason"}
                or output["level"] != rule_result["level"]
                or not isinstance(output["reason"], str) or not 1 <= len(output["reason"].strip()) <= 600):
            raise ValueError("解释结构或等级不符合规则")
        explanation = output["reason"].strip()
        entry.update(output=output, status="success")
    except APITimeoutError:
        entry.update(status="timeout", error="优先级解释超时，使用规则结果。")
    except APIError:
        entry.update(status="api_error", error="优先级解释调用失败，使用规则结果。")
    except (ValueError, TypeError):
        entry.update(status="invalid_response", error="优先级解释格式无效，使用规则结果。")
    except Exception:
        entry.update(status="unexpected_error", error="优先级解释暂不可用，使用规则结果。")
    finally:
        try:
            append_ai_call(entry)
        except Exception:
            # 日志辅助记录失败不改变已得到的规则结果或解释，也不触发重试。
            pass
    return explanation
