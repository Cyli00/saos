"""Zemax 操作的后端。后台进程只通过 Backend 接口操作镜头，单元测试换用 FakeBackend。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from saos import config
from saos.backend.base import SURFACE_PARAMS, Backend

if TYPE_CHECKING:
    from saos.backend.hybrid import HybridBackend


def make_backend(mode: str) -> Backend | HybridBackend:
    name = config.backend_name()
    if name == "fake":
        from saos.backend.fake import FakeBackend

        return FakeBackend()
    if name == "optiland" and mode != "extension":
        from saos.backend.hybrid import HybridBackend

        return HybridBackend()
    if name == "zos" or (name == "optiland" and mode == "extension"):
        from saos.backend.zos import ZosBackend

        return ZosBackend(mode=mode)
    raise ValueError(f"unknown backend: {name}")


__all__ = ["Backend", "SURFACE_PARAMS", "make_backend"]
