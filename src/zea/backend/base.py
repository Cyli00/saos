from __future__ import annotations

import math
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from zea.protocol import ErrorCode, ZeaError

NUMERIC_PARAMS = ("radius", "thickness", "conic", "semi_diameter")
TEXT_PARAMS = ("material", "comment")
SURFACE_PARAMS = NUMERIC_PARAMS + TEXT_PARAMS


def parse_numeric(param: str, value: Any) -> float:
    if isinstance(value, str) and value.strip().lower() in ("inf", "+inf", "infinity"):
        return math.inf
    if isinstance(value, str) and value.strip().lower() in ("-inf", "-infinity"):
        return -math.inf
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ZeaError(ErrorCode.BAD_ARGS, f"{param} 需要数值，收到 {value!r}") from exc
    if math.isnan(number):
        raise ZeaError(ErrorCode.BAD_ARGS, f"{param} 不能为 NaN")
    return number


def same_number(a: float, b: float) -> bool:
    if math.isinf(a) or math.isinf(b):
        return a == b
    return abs(a - b) <= 1e-12 * max(1.0, abs(a), abs(b))


class Backend(ABC):
    """单线程使用：所有方法都由后台进程的主循环依次调用。"""

    @abstractmethod
    def start(self) -> dict[str, Any]:
        """建立连接，返回版本等信息。失败时抛出 ZeaError。"""

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def healthy(self) -> bool: ...

    @abstractmethod
    def load(self, path: Path) -> None: ...

    @abstractmethod
    def save_as(self, path: Path) -> None: ...

    @abstractmethod
    def new_system(self) -> None: ...

    @abstractmethod
    def system_info(self) -> dict[str, Any]: ...

    @abstractmethod
    def surfaces(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def set_surface(self, index: int, param: str, value: Any) -> dict[str, Any]:
        """修改一个面参数并回读核对；未生效时抛出 VALUE_NOT_APPLIED。返回修改后的该面数据。"""

    @abstractmethod
    def operand(self, name: str, int1: int, int2: int, data: list[float]) -> float: ...

    # ---- 第 3 步：搭建与验收用 ----

    def build(self, prescription: dict[str, Any]) -> None:
        """按已规范化的处方（zea.prescription.normalize）从零搭建当前系统。"""
        raise ZeaError(ErrorCode.ZOS_ERROR, f"{type(self).__name__} 不支持 build")

    def snapshot(self) -> list[dict[str, Any]]:
        """当前系统所有面的完整数据，用于从设计文件重建验收模型。"""
        raise ZeaError(ErrorCode.ZOS_ERROR, f"{type(self).__name__} 不支持 snapshot")

    def zernike(self, *, wave: int, field: int, samp: int, max_term: int, terms: tuple[int, ...]) -> dict[int, float]:
        """一次拟合到 max_term 项（Zernike Standard，参考主光线），返回所需各项；负项号含义同 ZERN。"""
        raise ZeaError(ErrorCode.ZOS_ERROR, f"{type(self).__name__} 不支持 zernike")

    def trace_check(self, rays: list[tuple[int, float, float, float, float]]) -> list[dict[str, Any]]:
        """追迹到像面，返回失败或被截断的光线：(wave, hx, hy, px, py) 及 error/vignette 码。"""
        raise ZeaError(ErrorCode.ZOS_ERROR, f"{type(self).__name__} 不支持 trace_check")
