"""起点条目共用 entries/<id>/entry.json 与 prescription.json。"""
from __future__ import annotations

from pathlib import Path

from saos import config
from saos.protocol import ErrorCode, SaosError, read_json

ENTRY_ID_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.")


def entry_path(root: Path, entry_id: str) -> Path:
    if not entry_id or entry_id in (".", "..") or not set(entry_id) <= ENTRY_ID_CHARS:
        raise SaosError(ErrorCode.BAD_ARGS, "起点条目 ID 只能包含字母、数字和 -_.")
    return root / "entries" / entry_id


def entries(root: Path) -> list[dict]:
    return [read_json(p) for p in sorted((root / "entries").glob("*/entry.json"))]


def load_entry(root: Path, entry_id: str) -> dict:
    path = entry_path(root, entry_id) / "entry.json"
    if not path.is_file():
        raise SaosError(ErrorCode.FILE_NOT_FOUND, f"未找到起点条目 {entry_id}")
    return read_json(path)


def library_root() -> Path:
    return config.REPO_ROOT / "library" / "starts"
