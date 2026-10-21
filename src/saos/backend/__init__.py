"""Zemax 操作的后端。后台进程只通过 Backend 接口操作镜头，单元测试换用 FakeBackend。

认可前的任务只用 Optiland；OpticStudio 只在 `saos zos begin` 认可后由后台进程启动（见 daemon.enter_zos），
并发 worker 的 OpticStudio 会话由 parallel.backend_for 对认可后的任务显式指定。环境变量 SAOS_BACKEND 不能授权 OpticStudio。
"""

from __future__ import annotations

from saos import config
from saos.backend.base import SURFACE_PARAMS, Backend
from saos.protocol import ErrorCode, SaosError


def make_backend(mode: str, backend: str | None = None) -> Backend:
    """backend 缺省时读 SAOS_BACKEND，只接受 optiland 与 fake；zos 只能由调用方显式指定（认可后的 worker）。"""
    if backend is None:
        backend = config.backend_name()
        if backend not in ("optiland", "fake"):
            raise SaosError(ErrorCode.BAD_ARGS, f"SAOS_BACKEND={backend} cannot start a design task; only optiland (default) is accepted, "
                            "OpticStudio starts only through saos zos begin after the user accepts a report")
    if backend == "fake":
        from saos.backend.fake import FakeBackend

        return FakeBackend()
    if backend == "optiland":
        from saos.backend.optiland import OptilandBackend

        return OptilandBackend()
    if backend == "zos":
        from saos.backend.zos import ZosBackend

        return ZosBackend(mode=mode)
    raise SaosError(ErrorCode.BAD_ARGS, f"unknown backend: {backend}")


__all__ = ["Backend", "SURFACE_PARAMS", "make_backend"]
