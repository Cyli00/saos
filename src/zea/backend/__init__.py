"""Zemax 操作的后端。后台进程只通过 Backend 接口操作镜头，单元测试换用 FakeBackend。"""

from __future__ import annotations

from zea import config
from zea.backend.base import SURFACE_PARAMS, Backend


def make_backend(mode: str) -> Backend:
    name = config.backend_name()
    if name == "fake":
        from zea.backend.fake import FakeBackend

        return FakeBackend()
    if name == "zos":
        from zea.backend.zos import ZosBackend

        return ZosBackend(mode=mode)
    raise ValueError(f"未知的后端：{name}")


__all__ = ["Backend", "SURFACE_PARAMS", "make_backend"]
