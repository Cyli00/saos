"""命令行与后台进程之间的请求、响应格式和错误码。

请求：{"op": str, "args": dict}
响应：{"ok": true, "result": ...} 或 {"ok": false, "error": {"code", "message", "details"}}
"""

from __future__ import annotations

import json
import math
import numbers
import os
import tempfile
import time
from pathlib import Path
from typing import Any

# write_json 替换目标文件被拒时的重试次数和基础等待（第 k 次等 k 倍，合计约 1.4 s）
REPLACE_RETRIES = 8
REPLACE_WAIT_S = 0.05


class ErrorCode:
    BACKEND_UNSUPPORTED = "BACKEND_UNSUPPORTED"
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
        # numpy 标量和数组（kit 的追迹结果）
        return jsonable(value.tolist())
    return value


def write_json(path: Path, data: Any, *, strict: bool = False) -> None:
    """原子写入 JSON（先写临时文件再替换）。strict=True 时遇到 NaN/inf 直接报错，不做 jsonable 转换。"""
    text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) if strict else json.dumps(jsonable(data), ensure_ascii=False, indent=2)
    write_text(path, text)


def write_text(path: Path, text: str) -> None:
    """原子写入 UTF-8 文本：先写同目录的临时文件再替换，中途失败时目标文件保持原样。
    Write UTF-8 text atomically: a temporary file in the same folder replaces the target, which stays as it was on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        # Windows 上另一进程正打开目标文件读取时（流程图窗口轮询 progress.json、杀毒扫描），替换会报 WinError 5；
        # 读者很快就会关掉文件，短暂重试几次，仍失败才报错
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


def read_jsonl(path: Path) -> list[Any]:
    """逐行 JSON 文件的全部记录，文件不存在时为空。/ Every record of a JSON-lines file; empty when the file does not exist."""
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def finite(value: Any) -> float | None:
    """有限实数（含 numpy 数值）转成 float；None、布尔、非有限值、复数和文字都算读不出，返回 None。
    A finite real number (numpy included) as float; None, bools, non-finite values, complex numbers and text give None."""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


# 一个数的可信等级，由强到弱；refused 是没有数（当前后端算不了）
TRUST_LEVELS = ("measured", "reconciled", "unreconciled", "refused")


def trust(backend: str | None, provenance: dict[str, Any] | None = None) -> str | None:
    """一个数的可信等级：OpticStudio 算的是 measured；rtc 算的，经过未对账的计算路径或操作数时是 unreconciled，否则
    reconciled（来源记录带 unverified_affecting 时只看其中的路径）；没有来源记录的 rtc 数（来源记录出现之前的旧轮次）按 unreconciled 算；其他后端（测试用的 fake）为 None。
    旧记录没有 trust 字段时也用它从 backend 和 provenance 推出。
    A number's trust level: measured from OpticStudio; from rtc, unreconciled when it took a calculation path or operand
    not reconciled with OpticStudio (only the paths in unverified_affecting where the provenance has it), else reconciled; an rtc number without provenance (rounds older than provenance)
    counts as unreconciled; other backends (fake, for tests) give None. Also derives the level of old records."""
    if backend == "zos":
        return "measured"
    if backend != "rtc":
        return None
    if provenance is None:
        return "unreconciled"
    # 给了 unverified_affecting（这个数实际依赖的路径）时按它分档，否则按镜头的全部路径
    paths = provenance["unverified_affecting"] if "unverified_affecting" in provenance else provenance.get("unverified")
    return "unreconciled" if paths or provenance.get("operands_unverified") else "reconciled"


def stamp(backend: Any, single: bool = False) -> dict[str, Any]:
    """一次命令结果的来源与等级：computed_by 是后端身份，rtc 实例上另带 unverified（本镜头经过的未对账路径）与
    operands_unverified（自上次取走以来用到的未对账操作数，取走后清空）；single 为真（结果只来自单个操作数）时另带
    unverified_affecting；trust 按 trust() 分档。
    Source and level of one command's result: computed_by is the backend identity, on the rtc instance with unverified
    (unreconciled paths this lens takes) and operands_unverified (unreconciled operands used since last taken, then
    cleared), and with single (a result from one operand alone) unverified_affecting; trust is graded by trust()."""
    identity = backend.identity()
    standalone = getattr(backend, "standalone", False) and hasattr(backend, "provenance")
    provenance = (backend.provenance(single=True) if single else backend.provenance()) if standalone else None
    return {"computed_by": provenance or identity, "trust": trust(identity.get("backend"), provenance)}


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


class MaterialError(SaosError):
    """材料目录缺失或材料数据不可用。"""
