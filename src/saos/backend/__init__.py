"""ZOS 为正式计算后端；rtc 实例供需求阶段的可行性核算（不启动 OpticStudio）；fake 仅供工具测试使用。"""
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
    if backend == "rtc":
        # 需求阶段的可行性核算：不启动 OpticStudio，镜头由 rtc 持有 / need-stage feasibility without OpticStudio
        if mode != "standalone":
            raise SaosError(ErrorCode.BAD_ARGS, "the rtc instance has no OpticStudio window to connect to; start it without extension mode")
        from saos.backend.rtc import RtcBackend
        return RtcBackend(None, glass_dir=config.glass_dir(), standalone=True)
    raise SaosError(ErrorCode.BAD_ARGS, f"unsupported backend: {backend}; use zos (default), rtc (need-stage feasibility) or fake (tests only)")


__all__ = ["Backend", "SURFACE_PARAMS", "make_backend"]
