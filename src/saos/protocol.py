"""命令行与后台进程之间的请求、响应格式和错误码。

请求：{"op": str, "args": dict}
响应：{"ok": true, "result": ...} 或 {"ok": false, "error": {"code", "message", "details"}}
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any

# write_json 替换目标文件被拒时的重试次数和基础等待（第 k 次等 k 倍，合计约 1.4 s）
# retries and base wait when write_json's replace is refused (the k-th waits k times as long, about 1.4 s in all)
REPLACE_RETRIES = 8
REPLACE_WAIT_S = 0.05


class ErrorCode:
    BACKEND_UNSUPPORTED = "BACKEND_UNSUPPORTED"
    OPTILAND_ERROR = "OPTILAND_ERROR"
    LENS_COMPANION_MISMATCH = "LENS_COMPANION_MISMATCH"
    DAEMON_NOT_RUNNING = "DAEMON_NOT_RUNNING"
    DAEMON_ALREADY_RUNNING = "DAEMON_ALREADY_RUNNING"
    DAEMON_START_FAILED = "DAEMON_START_FAILED"
    DAEMON_TIMEOUT = "DAEMON_TIMEOUT"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    UNKNOWN_OP = "UNKNOWN_OP"
    BAD_ARGS = "BAD_ARGS"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_EXISTS = "JOB_EXISTS"
    PATH_OUTSIDE_JOB = "PATH_OUTSIDE_JOB"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    FILE_EXISTS = "FILE_EXISTS"
    NO_LENS_LOADED = "NO_LENS_LOADED"
    JOB_MISMATCH = "JOB_MISMATCH"
    UNSAVED_CHANGES = "UNSAVED_CHANGES"
    VALUE_NOT_APPLIED = "VALUE_NOT_APPLIED"
    VERIFY_FAILED = "VERIFY_FAILED"
    SCRIPT_ERROR = "SCRIPT_ERROR"
    UI_BLOCKED = "UI_BLOCKED"
    SPEC_CHANGED = "SPEC_CHANGED"
    DEMAND_NOT_CONFIRMED = "DEMAND_NOT_CONFIRMED"
    DEMAND_BLOCKED = "DEMAND_BLOCKED"
    CANCELLED = "CANCELLED"
    POOL_BUSY = "POOL_BUSY"
    BUSY = "BUSY"
    MODEL_NOT_APPROVED = "MODEL_NOT_APPROVED"
    LENS_MODIFIED_BY_CHECK = "LENS_MODIFIED_BY_CHECK"
    DUPLICATE_ROUND = "DUPLICATE_ROUND"
    EVIDENCE_TAMPERED = "EVIDENCE_TAMPERED"
    ZOS_ERROR = "ZOS_ERROR"
    ZOS_LOAD_FAILED = "ZOS_LOAD_FAILED"
    ZOS_SAVE_FAILED = "ZOS_SAVE_FAILED"
    ZOS_LICENSE = "ZOS_LICENSE"
    ZOS_VERSION_MISMATCH = "ZOS_VERSION_MISMATCH"
    ZOS_CONNECTION_LOST = "ZOS_CONNECTION_LOST"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class SaosError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def ok(result: Any) -> dict[str, Any]:
    return {"ok": True, "result": result}


def fail(err: SaosError) -> dict[str, Any]:
    return {"ok": False, "error": err.to_dict()}


def jsonable(value: Any) -> Any:
    """把 inf/nan 换成 JSON 能表示的值：±inf → "inf"/"-inf"，nan → None。"""
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return value
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if type(value).__module__ == "numpy" and hasattr(value, "tolist"):
        # numpy 标量和数组（kit 的追迹结果）/ numpy scalars and arrays (kit's trace results)
        return jsonable(value.tolist())
    return value


def write_json(path: Path, data: Any, *, strict: bool = False) -> None:
    """原子写入 JSON（先写临时文件再替换）。strict=True 时遇到 NaN/inf 直接报错，不做 jsonable 转换。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) if strict else json.dumps(jsonable(data), ensure_ascii=False, indent=2)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as fh:
        tmp = Path(fh.name)
        fh.write(text)
    # Windows 上另一进程正打开目标文件读取时（流程图窗口轮询 progress.json、杀毒扫描），替换会报 WinError 5；
    # 读者很快就会关掉文件，短暂重试几次，仍失败才报错
    # on Windows, replacing a file another process holds open for reading (the workflow window polling progress.json,
    # a virus scan) fails with WinError 5; readers close it quickly, so retry briefly before giving up
    try:
        for attempt in range(REPLACE_RETRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == REPLACE_RETRIES - 1:
                    raise
                time.sleep(REPLACE_WAIT_S * (attempt + 1))
    finally:
        tmp.unlink(missing_ok=True)


def read_json(path: Path | str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SaosError(ErrorCode.BAD_ARGS, f"cannot read JSON file {path}: {exc}") from exc


def encode(message: dict[str, Any]) -> bytes:
    return json.dumps(jsonable(message), ensure_ascii=False).encode("utf-8")


def decode(raw: bytes) -> dict[str, Any]:
    try:
        message = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SaosError(ErrorCode.PROTOCOL_ERROR, f"cannot parse message: {exc}") from exc
    if not isinstance(message, dict):
        raise SaosError(ErrorCode.PROTOCOL_ERROR, "messages must be JSON objects")
    return message
