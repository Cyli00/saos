"""ZOS 为正式计算后端；fake 仅供工具测试使用。"""
from saos import config
from saos.backend.base import SURFACE_PARAMS, Backend
from saos.protocol import ErrorCode, SaosError


def make_backend(mode: str, backend: str | None = None) -> Backend:
    backend = config.backend_name() if backend is None else backend
    if backend == "fake":
        from saos.backend.fake import FakeBackend
        return FakeBackend()
    if backend == "zos":
        from saos.backend.zos import ZosBackend
        return ZosBackend(mode=mode)
    raise SaosError(ErrorCode.BAD_ARGS, f"unsupported backend: {backend}; use zos (default) or fake (tests only)")


__all__ = ["Backend", "SURFACE_PARAMS", "make_backend"]
