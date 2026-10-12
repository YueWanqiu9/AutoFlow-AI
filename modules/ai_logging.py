"""本地 AI 调用日志与统计；原子替换文件，避免写入半截 JSON。"""

import json
import math
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock

LOG_PATH = Path(__file__).resolve().parents[1] / "logs" / "ai_calls.json"
_LOG_LOCK = Lock()


def _read_calls() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    calls = json.loads(LOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(calls, list) or any(not isinstance(call, dict) for call in calls):
        raise ValueError("AI 调用日志必须是 JSON 对象数组。")
    return calls


def read_ai_calls() -> list[dict]:
    with _LOG_LOCK:
        return _read_calls()


def append_ai_call(entry: dict) -> None:
    """追加一条记录；旧日志损坏时拒绝覆盖，由调用方处理失败。"""
    with _LOG_LOCK:
        calls = _read_calls()
        calls.append(entry)
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=LOG_PATH.parent, suffix=".tmp", delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(calls, temporary, ensure_ascii=False, indent=2, allow_nan=False)
                temporary.write("\n")
            temporary_path.replace(LOG_PATH)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def get_ai_call_stats() -> dict:
    """统计真实 SDK 尝试、解析成功率和平均秒数，演示与配置记录不计入。"""
    calls = [
        call for call in read_ai_calls()
        if call.get("status") not in ("demo", "configuration_error")
        and call.get("api_called", True) is True
    ]
    times = []
    for call in calls:
        value = call.get("response_time")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        try:
            seconds = float(value)
        except OverflowError:
            continue
        if math.isfinite(seconds) and seconds >= 0:
            times.append(seconds)
    successful = sum(call.get("status") == "success" for call in calls)
    return {
        "ai_call_count": len(calls),
        "avg_response_time": sum(times) / len(times) if times else None,
        "ai_success_count": successful,
        "ai_success_rate": successful / len(calls) if calls else None,
    }
