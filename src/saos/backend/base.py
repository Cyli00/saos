from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from saos.protocol import ErrorCode, SaosError

NUMERIC_PARAMS = ("radius", "thickness", "conic", "semi_diameter")
TEXT_PARAMS = ("material", "comment")
SURFACE_PARAMS = NUMERIC_PARAMS + TEXT_PARAMS
# 面型参数列（非球面系数、坐标断点的偏心和倾斜、菲涅尔基底等），按 Lens Data 的 Par 列号
# surface-type parameter columns (aspheric terms, coordinate-break decenters and tilts, …) by their Lens Data Par number
PAR_RE = re.compile(r"^par([1-9]\d{0,2})$")
# 可设为变量的量：曲率半径、厚度与间隔、圆锥、面型参数（李晓彤 17.1：结构参数为曲率半径或非球面系数、厚度和间隔、折射率），以及材料
# what can vary: radius, thickness and spacing, conic, surface-type parameters (Li Xiaotong 17.1: the structural parameters are
# the radii or aspheric terms, the thicknesses and spacings and the indices), and the material
VARY_PARAMS = ("radius", "thickness", "conic", "par<N>", "material")
MATERIAL_MODES = ("model", "substitute")


def is_par(param: str) -> bool:
    return bool(PAR_RE.match(str(param)))


def check_param(param: str, allowed: tuple[str, ...] = SURFACE_PARAMS) -> None:
    if param not in allowed and not is_par(param):
        raise SaosError(ErrorCode.BAD_ARGS, f"unsupported surface parameter {param!r}; use one of {', '.join(allowed)} or par<N> (a Lens Data parameter column)")


def parse_numeric(param: str, value: Any) -> float:
    if isinstance(value, str) and value.strip().lower() in ("inf", "+inf", "infinity"):
        return math.inf
    if isinstance(value, str) and value.strip().lower() in ("-inf", "-infinity"):
        return -math.inf
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SaosError(ErrorCode.BAD_ARGS, f"{param} needs a number, got {value!r}") from exc
    if math.isnan(number):
        raise SaosError(ErrorCode.BAD_ARGS, f"{param} cannot be NaN")
    return number


def same_number(a: float, b: float) -> bool:
    if math.isinf(a) or math.isinf(b):
        return a == b
    return abs(a - b) <= 1e-12 * max(1.0, abs(a), abs(b))


class Backend(ABC):
    """单线程使用：所有方法都由后台进程的主循环依次调用。"""

    @abstractmethod
    def start(self) -> dict[str, Any]:
        """建立连接，返回版本等信息。失败时抛出 SaosError。"""

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

    @contextmanager
    def activity(self, label: str, quiet: bool):
        """一次请求期间的界面提示与刷新控制；只在连接手动打开的窗口时起作用。"""
        yield

    def needs_save(self) -> bool:
        """OpticStudio 的 NeedsSave 标志。实测只读的 Zernike 拟合、把参数写回相同的值也会让它置位。"""
        return False

    def has_unsaved_changes(self) -> bool:
        """连接的窗口里有 saos 之外的未保存修改。只在 extension 模式下有意义。"""
        return False

    def analysis_types(self) -> list[str]:
        raise SaosError(ErrorCode.ZOS_ERROR, f"{type(self).__name__} has no analyses")

    def analysis(self, name: str, settings: dict[str, Any], text_path: Path, keep_open: bool = False) -> dict[str, Any]:
        """运行一个 OpticStudio 分析，返回导出的序列、网格、点列图数据和表头；完整文本写到 text_path。"""
        raise SaosError(ErrorCode.ZOS_ERROR, f"{type(self).__name__} has no analyses")

    def surface_value(self, index: int, param: str) -> Any:
        """单个面参数的当前值（含 par<N> 面型参数）。/ The current value of one surface parameter (par<N> included)."""
        return self.surfaces()[index].get(param)

    def vary(self, surface: int, param: str, on: bool, mode: str | None = None, catalog: str | None = None) -> str:
        """设变量或取消变量；material 用 mode 选模型玻璃（nd、νd 可变）或目录内替换。返回求解类型。
        Make a parameter variable or fixed; for material, mode picks a model glass (nd and Vd vary) or catalog substitution."""
        raise SaosError(ErrorCode.ZOS_ERROR, f"{type(self).__name__} does not support vary")

    def show_windows(self, viewer: str | None = None, analysis: str | None = None, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        """显性调试：在连接的窗口中打开 Lens Data 和一个布局窗口（剖面图画不了时用 3D 视图），按需再开一个像质分析。"""
        raise SaosError(ErrorCode.BAD_ARGS, "explicit debugging windows need extension mode (saos daemon start --mode extension)")

    def refresh_windows(self) -> dict[str, Any] | None:
        """改动镜头后刷新显性调试的窗口；隐式运行时什么也不做。"""
        return None

    def blocking_dialogs(self) -> list[str]:
        """显性调试：当前挡住窗口的模态对话框标题；隐式运行时为空。"""
        return []

    def screenshots(self, folder: Path, prefix: str) -> list[str]:
        """把连接的 OpticStudio 的可见窗口截成 PNG，返回文件名。"""
        raise SaosError(ErrorCode.BAD_ARGS, "screenshots need extension mode")

    def describe_model(self, folder: Path | None, prefix: str = "layout") -> dict[str, Any]:
        """建模说明所需的全部数据（系统设置、逐面参数与材料、各波长折射率、孔径、求解）；folder 不为空时把布局图写到那里。
        Everything a model description needs; layout pictures are written to folder unless it is None."""
        raise SaosError(ErrorCode.ZOS_ERROR, f"{type(self).__name__} does not support describe_model")

    # ---- 第 3 步：搭建与验收用 ----

    def build(self, prescription: dict[str, Any]) -> None:
        """按已规范化的处方（saos.prescription.normalize）从零搭建当前系统。"""
        raise SaosError(ErrorCode.ZOS_ERROR, f"{type(self).__name__} does not support build")

    def snapshot(self) -> list[dict[str, Any]]:
        """当前系统所有面的完整数据，用于从设计文件重建验收模型。"""
        raise SaosError(ErrorCode.ZOS_ERROR, f"{type(self).__name__} does not support snapshot")

    def zernike(self, *, wave: int, field: int, samp: int, max_term: int, terms: tuple[int, ...]) -> dict[int, float]:
        """一次拟合到 max_term 项（Zernike Standard，参考主光线），返回所需各项；负项号含义同 ZERN。"""
        raise SaosError(ErrorCode.ZOS_ERROR, f"{type(self).__name__} does not support zernike")

    def viz_data(self, folder: Path | None, *, full: bool = True) -> dict[str, Any]:
        """saos ui 的数据：逐面全局几何、布局光线、Lens Data、交叠检查；full 时另有点列图光线和分析导出（文本写到 folder）。
        Data for saos ui: global geometry, layout rays, Lens Data, interference; with full also spot rays and analysis exports."""
        raise SaosError(ErrorCode.ZOS_ERROR, f"{type(self).__name__} does not support viz_data")

    def interference(self) -> dict[str, Any]:
        """结构交叠检查：面相交（crossings）、镜片穿插（overlaps）。/ Interference: crossing surfaces and interpenetrating lenses."""
        raise SaosError(ErrorCode.ZOS_ERROR, f"{type(self).__name__} does not support interference")

    def trace_check(self, rays: list[tuple[int, float, float, float, float]]) -> list[dict[str, Any]]:
        """追迹到像面，返回失败或被截断的光线：(wave, hx, hy, px, py) 及 error/vignette 码。"""
        raise SaosError(ErrorCode.ZOS_ERROR, f"{type(self).__name__} does not support trace_check")
