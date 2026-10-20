from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from saos.protocol import ErrorCode, SaosError

COMPANION_SCHEMA = 2
CONTENT_MARKER = "SAOS_OPTILAND_SHA256"


def companion(path: Path) -> Path:
    return path.with_suffix(".optiland.json")


def payload_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=True, separators=(",", ":")).encode("utf-8")


def read_companion(path: Path) -> dict | None:
    side = companion(path)
    if not side.exists():
        return None
    try:
        data = json.loads(side.read_text(encoding="utf-8"))
        primary = path.read_bytes()
        payload = data["payload"]
        if data["schema"] != COMPANION_SCHEMA or data["zmx_sha256"] != hashlib.sha256(primary).hexdigest():
            raise ValueError("schema or primary file hash differs")
        if data["payload_sha256"] != hashlib.sha256(payload_bytes(payload)).hexdigest():
            raise ValueError("the companion's contents were changed after it was written")
        return payload
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SaosError(ErrorCode.LENS_COMPANION_MISMATCH,
                        f"lens and companion do not match: {path}: {exc}. If the user edited the .zmx in OpticStudio on purpose, "
                        "ask them to save it there under a new name and lens import that file (it opens in OpticStudio); "
                        "otherwise restore both files from the last save. Never delete the companion yourself",
                        {"lens": str(path), "companion": str(side)}) from exc


def companion_bytes(primary: bytes, payload: dict) -> bytes:
    """伴随文件绑定 .zmx 的哈希和自身内容的哈希；.zmx 可以由 Optiland 或 OpticStudio 写出（编码不限）。
    The companion binds the hash of the .zmx and of its own payload; the .zmx may come from Optiland or OpticStudio."""
    return json.dumps({"schema": COMPANION_SCHEMA, "zmx_sha256": hashlib.sha256(primary).hexdigest(),
                       "payload_sha256": hashlib.sha256(payload_bytes(payload)).hexdigest(), "payload": payload},
                      ensure_ascii=False, allow_nan=True, indent=2).encode("utf-8")


def _temporary(path: Path, content: bytes) -> Path:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        return temp
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def write_pair(path: Path, primary: bytes, side: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    targets = (path, companion(path))
    previous = [p.read_bytes() if p.exists() else None for p in targets]
    pending: list[Path] = []
    replaced = 0
    try:
        pending = [_temporary(path, primary)]
        pending.append(_temporary(targets[1], side))
        for temp, target in zip(pending, targets):
            os.replace(temp, target)
            replaced += 1
    except BaseException:
        # 双文件无法一次原子替换；第二次替换失败时恢复已替换的文件。
        for target, before in zip(targets[:replaced], previous[:replaced]):
            if before is None:
                target.unlink(missing_ok=True)
            else:
                restore = _temporary(target, before)
                try:
                    os.replace(restore, target)
                finally:
                    restore.unlink(missing_ok=True)
        raise
    finally:
        for temp in pending:
            temp.unlink(missing_ok=True)


def copy_lens(source: Path, target: Path) -> None:
    payload = read_companion(source)
    if payload is not None:
        write_pair(target, source.read_bytes(), companion(source).read_bytes())
    else:
        shutil.copy2(source, target)
        companion(target).unlink(missing_ok=True)
