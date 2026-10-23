"""ZOS 保存的 .zmx 是权威镜头；旧 Optiland 快照只检测存在，不读写或转换。"""
from __future__ import annotations

import shutil
from pathlib import Path

from saos.protocol import ErrorCode, SaosError


def companion(path: Path) -> Path:
    return path.with_suffix(".optiland.json")


def require_native(path: Path) -> None:
    if path.name.endswith(".optiland.json") or companion(path).exists():
        raise SaosError(ErrorCode.BACKEND_UNSUPPORTED,
                        "legacy Optiland snapshots cannot be loaded or overwritten; preserve the original files, "
                        "rebuild from the original prescription in a new job or import a .zmx saved by OpticStudio "
                        "under a new name, then measure again", {"lens": str(path)})


def exists(path: Path) -> bool:
    return path.is_file() or companion(path).exists()


def files(path: Path) -> list[Path]:
    require_native(path)
    return [path] if path.is_file() else []


def all_lenses(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() == ".zmx")


def listing(folder: Path, pattern: str) -> list[Path]:
    return sorted(p for p in folder.glob(pattern) if p.is_file())


def copy_lens(source: Path, target: Path) -> None:
    require_native(source)
    require_native(target)
    shutil.copy2(source, target)
