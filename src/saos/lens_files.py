"""镜头的原生快照（伴随文件 `<name>.optiland.json`）与可选的 `.zmx` 导出。

schema 3：快照独立保存。`.zmx` 存在时哈希必须与快照记录一致（导出件）；不存在时快照单独有效，
表示 Optiland 写不出准确的 `.zmx`，此时不生成占位文件。
schema 2：旧的双文件，`.zmx` 必须存在且哈希一致；只读兼容，不再由 Optiland 写出。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from saos.protocol import ErrorCode, SaosError

COMPANION_SCHEMA = 3
LEGACY_PAIR_SCHEMA = 2
CONTENT_MARKER = "SAOS_OPTILAND_SHA256"
SNAPSHOT_SUFFIX = ".optiland.json"


def companion(path: Path) -> Path:
    return path.with_suffix(SNAPSHOT_SUFFIX)


def payload_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=True, separators=(",", ":")).encode("utf-8")


def read_companion(path: Path) -> dict | None:
    side = companion(path)
    if not side.exists():
        return None
    try:
        data = json.loads(side.read_text(encoding="utf-8"))
        payload = data["payload"]
        if data["schema"] not in (COMPANION_SCHEMA, LEGACY_PAIR_SCHEMA):
            raise ValueError("unsupported companion schema")
        # 快照和 .zmx 是否成对，取决于 .zmx 是否存在；两者不一致说明有人单独改动了其中一个
        has_primary = path.is_file()
        if has_primary != (data["zmx_sha256"] is not None):
            raise ValueError("the .zmx and its companion do not belong together")
        if has_primary and data["zmx_sha256"] != hashlib.sha256(path.read_bytes()).hexdigest():
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


def companion_bytes(primary: bytes | None, payload: dict) -> bytes:
    """快照绑定自身内容的哈希；有 .zmx 时还绑定它的哈希。.zmx 可以由 Optiland 或 OpticStudio 写出（编码不限）。
    The snapshot binds its own payload hash; with a .zmx it also binds the .zmx hash (written by Optiland or OpticStudio)."""
    return json.dumps({"schema": COMPANION_SCHEMA,
                       "zmx_sha256": None if primary is None else hashlib.sha256(primary).hexdigest(),
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


def exists(path: Path) -> bool:
    """镜头产物是否存在：.zmx 或原生快照任一存在即算，快照可以单独存在。 / A lens exists when its .zmx or its snapshot does."""
    return path.is_file() or companion(path).is_file()


def files(path: Path) -> list[Path]:
    """镜头的全部实际文件；封存、复制、移动、删除都以它为准，不再用后缀推断。
    Every file that actually exists for a lens; sealing, copying, moving and deleting all use this list."""
    return [p for p in (path, companion(path)) if p.is_file()]


def primary_of(side: Path) -> Path:
    """快照对应的镜头路径（.zmx 名）。 / The lens path (.zmx name) of a snapshot file."""
    return side.with_name(side.name[: -len(SNAPSHOT_SUFFIX)] + ".zmx")


def all_lenses(folder: Path) -> list[Path]:
    """文件夹内（递归）的全部镜头，包括只有快照的。 / Every lens under folder, recursively, including snapshot-only ones."""
    found = set()
    for p in folder.rglob("*"):
        if p.name.endswith(SNAPSHOT_SUFFIX):
            found.add(primary_of(p))
        elif p.suffix.lower() == ".zmx":
            found.add(p)
    return sorted(p for p in found if exists(p))


def listing(folder: Path, pattern: str) -> list[Path]:
    """按 .zmx 名的通配模式列出镜头，包括只有快照的镜头；返回 .zmx 路径。
    Lenses matching a .zmx glob pattern, including snapshot-only ones; returned as .zmx paths."""
    side_pattern = pattern[: -len(".zmx")] + SNAPSHOT_SUFFIX
    found = {primary_of(p) if p.name.endswith(SNAPSHOT_SUFFIX) else p for p in (*folder.glob(pattern), *folder.glob(side_pattern))}
    return sorted(p for p in found if exists(p))


def write_snapshot(path: Path, side: bytes) -> None:
    """只写快照。原位置的旧 .zmx 属于被覆盖的旧版本，留着会与新快照不配对，所以先删旧 .zmx、再替换快照；
    任一步失败都把旧 .zmx 与旧快照写回，旧镜头保持原样配对。
    Write the snapshot only. An old .zmx at the same place belongs to the replaced version and would not pair with the new
    snapshot, so it goes too. If any step fails, the old .zmx and the old snapshot are written back."""
    path.parent.mkdir(parents=True, exist_ok=True)
    old_primary = path.read_bytes() if path.is_file() else None
    temp = _temporary(companion(path), side)
    try:
        path.unlink(missing_ok=True)
        os.replace(temp, companion(path))
    except BaseException:
        if old_primary is not None and not path.exists():
            restore = _temporary(path, old_primary)
            try:
                os.replace(restore, path)
            finally:
                restore.unlink(missing_ok=True)
        raise
    finally:
        temp.unlink(missing_ok=True)


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
    if payload is None:
        shutil.copy2(source, target)
        companion(target).unlink(missing_ok=True)
    elif source.is_file():
        write_pair(target, source.read_bytes(), companion(source).read_bytes())
    else:
        write_snapshot(target, companion(source).read_bytes())
