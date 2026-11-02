from __future__ import annotations

import math
import os
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from saos import lensdata
from saos.lensdata import PAR_RE
from saos.protocol import ErrorCode, SaosError

# mech_semi_diameter：零件外缘（Lens Data 的 Mech Semi-Dia 列），交叠检查按它取零件边缘 / the part edge the interference check uses
NUMERIC_PARAMS = ("radius", "thickness", "conic", "semi_diameter", "mech_semi_diameter")
TEXT_PARAMS = ("material", "comment")
SURFACE_PARAMS = NUMERIC_PARAMS + TEXT_PARAMS
MATERIAL_MODES = ("model", "substitute")
# 不指定目录时玻璃替换用的目录：成都光明的在产牌号（2026-10-19 用户决定）/ the default catalog for glass substitution
DEFAULT_SUBSTITUTE_CATALOG = "SAOS_CDGM_INPROD"


def check_field(field: Any) -> int | tuple[float, float]:
    """PSF 与波前分析的视场：正整数视场号，或两个有限数 (x, y)。/ A field for PSF and wavefront analyses."""
    if isinstance(field, int) and not isinstance(field, bool):
        if field >= 1:
            return field
    elif isinstance(field, (tuple, list)) and len(field) == 2:
        try:
            x, y = float(field[0]), float(field[1])
        except (TypeError, ValueError):
            x = y = math.nan
        if math.isfinite(x) and math.isfinite(y):
            return x, y
    raise SaosError(ErrorCode.BAD_ARGS, f"field must be a field number (1, 2, ...) or (x, y) in the lens's field units, got {field!r}")


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
    if isinstance(value, bool):
        # float(True) 是 1.0，会把写错的参数悄悄当成数值 / float(True) is 1.0 and would pass a mistaken argument as a number
        raise SaosError(ErrorCode.BAD_ARGS, f"{param} needs a number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SaosError(ErrorCode.BAD_ARGS, f"{param} needs a number, got {value!r}") from exc
    if math.isnan(number):
        raise SaosError(ErrorCode.BAD_ARGS, f"{param} cannot be NaN")
    return number


def check_size(param: str, number: float) -> None:
    """半口径和机械半口径是尺寸：有限且不为负。/ Semi-diameters are sizes: finite and not negative."""
    if param in ("semi_diameter", "mech_semi_diameter") and not (math.isfinite(number) and number >= 0):
        raise SaosError(ErrorCode.BAD_ARGS, f"{param} must be a finite size >= 0, got {number}")


def same_path(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def needs_opticstudio(backend, what: str) -> SaosError:
    """只有 OpticStudio 才有的功能：rtc 上算作当前后端算不了（判定的 backend_unsupported），其他后端仍是参数错。
    A feature only OpticStudio has: a backend gap on rtc (backend_unsupported in checks), a bad call elsewhere."""
    code = ErrorCode.BACKEND_UNSUPPORTED if backend.identity().get("backend") == "rtc" else ErrorCode.BAD_ARGS
    return SaosError(code, f"{what} needs OpticStudio; {type(backend).__name__} has none")


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

    def identity(self) -> dict[str, Any]:
        """本实例自己的身份（后端名、版本、精度），取自它自己的 start()；未知的字段为 None，不借用别的实例的值。
        This instance's own identity (backend name, version, precision), from its own start(); unknown fields are None,
        never another instance's values."""
        return {"backend": None, "version": None, "precision": None}

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

    def objects(self) -> list[dict[str, Any]]:
        """非序列物体表；不支持非序列的后端返回空。/ Non-sequential objects; empty on backends without them."""
        return []

    @abstractmethod
    def set_surface(self, index: int, param: str, value: Any) -> dict[str, Any]:
        """修改一个面参数并回读核对；未生效时抛出 VALUE_NOT_APPLIED。返回修改后的该面数据。"""

    @abstractmethod
    def operand(self, name: str, int1: int, int2: int, data: list[float]) -> float: ...

    @contextmanager
    def activity(self, label: str, quiet: bool):
        """一次请求期间的界面提示与刷新控制；只在连接手动打开的窗口时起作用。"""
        yield

    def native_file(self) -> Path | None:
        """后端自己记着的镜头文件（OpticStudio 的 SystemFile）。Hammer 与全局搜索把结果写在它旁边；从临时快照恢复后它指向快照。"""
        return None

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

    def surface_count(self) -> int:
        """面数（含物面与像面）。/ The number of surfaces, object and image included."""
        return len(self.surfaces())

    def surface_comments(self) -> dict[str, int]:
        """注释 → 面序号（重名时取第一个，空注释不列）。/ comment → surface index (the first on repeats; empty ones left out)."""
        found: dict[str, int] = {}
        for s in self.surfaces():
            found.setdefault(str(s.get("comment", "")), int(s["index"]))
        found.pop("", None)
        return found

    def get_param(self, key: str) -> Any:
        kind, ref, param = lensdata.parse_key(key)
        if kind != "surface":
            raise SaosError(ErrorCode.BACKEND_UNSUPPORTED, f"{type(self).__name__} cannot read {key}")
        value = self.surface_value(lensdata.surface(self, ref), "radius" if param == "curvature" else param)
        return (0.0 if value == 0 or math.isinf(value) else 1.0 / value) if param == "curvature" else value

    def set_param(self, key: str, value: Any) -> None:
        kind, ref, param = lensdata.parse_key(key)
        if kind != "surface":
            raise SaosError(ErrorCode.BACKEND_UNSUPPORTED, f"{type(self).__name__} cannot write {key}")
        if param == "curvature":
            param, value = "radius", math.inf if float(value) == 0 else 1.0 / float(value)
        self.set_surface(lensdata.surface(self, ref), param, value)

    # ---- 只有 OpticStudio 有的 / OpticStudio only ----

    def zosapi(self):
        """ZOS-API 模块（脚本直接调 ZOS-API 时用）。/ The ZOS-API module, for scripts that call ZOS-API directly."""
        raise needs_opticstudio(self, "zosapi")

    def surface_params(self, ref: int | str) -> list[dict[str, Any]]:
        """该面型的参数列：par、name、value、read_only。/ The surface type's parameter columns."""
        raise needs_opticstudio(self, "surface_params")

    def object_index(self, ref: int | str) -> int:
        """非序列物体的序号（序号或注释，混合模式写 <物体>@<面号>）。/ A non-sequential object's number."""
        raise needs_opticstudio(self, "object")

    def nsc_objects(self) -> list[dict[str, Any]]:
        """纯非序列系统的物体表；序列系统报 BAD_ARGS。/ The objects of a pure non-sequential system."""
        raise needs_opticstudio(self, "objects")

    def nsc_trace(self, **options: Any) -> dict[str, Any]:
        """非序列追迹，参数与返回值同 saos.backend.nsc.trace。/ A non-sequential trace, as saos.backend.nsc.trace."""
        raise needs_opticstudio(self, "nsc_trace")

    def detector(self, ref: int | str, grid: bool = False) -> dict[str, Any]:
        raise needs_opticstudio(self, "detector")

    def ray_hits(self, zrd: str, refs: list[int | str], ray_filter: str = "", max_rays: int | None = None) -> dict[str, list]:
        """读光线数据库（相对路径以镜头文件所在目录为准），返回各列表与 rays_read。/ Read a ray database."""
        raise needs_opticstudio(self, "ray_hits")

    def trace_batch(self, rays, to: int):
        raise SaosError(ErrorCode.BACKEND_UNSUPPORTED, f"{type(self).__name__} has no real ray tracer")

    def global_matrix(self, index: int):
        raise SaosError(ErrorCode.BACKEND_UNSUPPORTED, f"{type(self).__name__} has no surface transforms")

    def sag(self, index: int, x: float, y: float) -> float | None:
        raise SaosError(ErrorCode.BACKEND_UNSUPPORTED, f"{type(self).__name__} has no surface geometry")

    def primary_wave(self) -> int:
        return next((i for i, w in enumerate(self.system_info()["wavelengths"], 1) if w["primary"]), 1)

    def wavelength_count(self) -> int:
        return len(self.system_info()["wavelengths"])

    def configuration_count(self) -> int:
        return 1

    def current_configuration(self) -> int:
        return 1

    def select_configuration(self, n: int) -> None:
        if isinstance(n, bool) or not isinstance(n, int) or n != 1:
            raise SaosError(ErrorCode.BAD_ARGS, "this backend has only configuration 1")

    def substitute_catalog(self, catalog: str | None) -> str:
        """玻璃替换实际使用的目录名（不改镜头）；空串表示当前使用的全部目录。
        The catalog glass substitution will use, without touching the lens; empty means every catalog in use."""
        return (catalog or "").upper()

    def catalog_has(self, name: str, catalog: str) -> bool | None:
        """牌号是否在这个目录里；不知道时为 None。/ Whether a glass is in this catalog; None when unknown."""
        return None

    def vary(self, surface: int, param: str, on: bool, mode: str | None = None, catalog: str | None = None) -> str:
        """设变量或取消变量；material 用 mode 选模型玻璃（nd、νd 可变）或目录内替换。返回求解类型。
        catalog 是 substitute_catalog 已解析的目录名，原样使用，空串表示全部在用目录。
        Make a parameter variable or fixed; for material, mode picks a model glass (nd and Vd vary) or catalog substitution.
        catalog is the name substitute_catalog resolved, used as given; empty means every catalog in use."""
        raise SaosError(ErrorCode.ZOS_ERROR, f"{type(self).__name__} does not support vary")

    def merit_append(self, rows: list[dict[str, Any]]) -> list[int]:
        """按输入顺序把评价函数行插在第一个 DMFS 之前（没有时接在末尾），返回新行号；行格式见 saos.ctx.rows.compile_rows。
        Insert merit rows, in order, before the first DMFS (at the end without one); returns their row numbers."""
        raise SaosError(ErrorCode.BACKEND_UNSUPPORTED, f"{type(self).__name__} has no merit function")

    def insert_surface(self, index: int, surface_type: str = "Standard") -> str:
        """在 index 处插入一个面（像面之前），返回面型。/ Insert a surface at index (before the image); returns its type."""
        raise SaosError(ErrorCode.BACKEND_UNSUPPORTED, f"{type(self).__name__} cannot insert surfaces")

    def change_surface_type(self, index: int, surface_type: str) -> str:
        raise SaosError(ErrorCode.BACKEND_UNSUPPORTED, f"{type(self).__name__} cannot change surface types")

    def set_solve(self, index: int, param: str, solve: dict[str, Any]) -> dict[str, Any]:
        """设一个单元格的求解（fixed、variable、pickup、marginal_ray_height、f_number），返回回读的求解。"""
        raise SaosError(ErrorCode.BACKEND_UNSUPPORTED, f"{type(self).__name__} cannot set solves")

    def vary_config(self, rows: list[int], configs: list[int] | None, on: bool) -> dict[str, str]:
        """把多重结构的格设为变量或固定；configs 为空时是全部结构。返回 {"<行>.<结构>": 求解类型}。
        Make multi-configuration cells variable or fixed; configs empty means every configuration."""
        raise SaosError(ErrorCode.ZOS_ERROR, f"{type(self).__name__} has no multiple configurations")

    def show_windows(self, viewer: str | None = None, analysis: str | None = None, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        """显性调试：在连接的窗口中打开 Lens Data 和一个布局窗口（剖面图画不了时用 3D 视图），按需再开一个像质分析。"""
        raise SaosError(ErrorCode.BAD_ARGS, f"{type(self).__name__} has no OpticStudio windows; use a ZOS session")

    def refresh_windows(self) -> dict[str, Any] | None:
        """改动镜头后刷新显性调试的窗口；隐式运行时什么也不做。"""
        return None

    def blocking_dialogs(self) -> list[str]:
        """显性调试：当前挡住窗口的模态对话框标题；隐式运行时为空。"""
        return []

    def screenshots(self, folder: Path, prefix: str) -> list[str]:
        """把连接的 OpticStudio 的可见窗口截成 PNG，返回文件名。"""
        raise SaosError(ErrorCode.BAD_ARGS, f"{type(self).__name__} has no OpticStudio windows; use a ZOS session for screenshots")

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

    def huygens(self, wave: int, pupil: int, image: int) -> Any:
        """Huygens PSF 会话：psf(field, delta_um) 返回 (ny×nx 的 numpy 强度数组, dx_um, dy_um) 或 None（分析没有数据），close() 结束。
        field 为视场号，或镜头视场单位下的 (x, y)（会话期间追加到视场表，close 时删掉）。强度不归一化，同一会话内可比。
        A Huygens PSF session: psf(field, delta_um) gives (an ny×nx numpy intensity array, dx_um, dy_um) or None (no data), close()
        ends it. field is a field number or (x, y) in the lens's field units (appended for the session). Intensities are
        not normalised and compare within one session."""
        raise SaosError(ErrorCode.ZOS_ERROR, f"{type(self).__name__} does not support huygens")

    def wavefront_rms(self, wave: int, fields: list[int | tuple[float, float]]) -> list[float | None]:
        """各视场光线积分的 RMS 波前误差（波长，参考质心即扣除平移与倾斜）；算不出的为 None。视场写法同 huygens。
        RMS wavefront error from ray integration per field (waves, to centroid: piston and tilt removed); None where not computable."""
        raise SaosError(ErrorCode.ZOS_ERROR, f"{type(self).__name__} does not support wavefront_rms")

    def surface_aperture(self, index: int) -> dict[str, Any]:
        """面孔径设置：type（CircularAperture、FloatingAperture、None …）及 MinimumRadius、MaximumRadius 等数值。
        A surface's aperture: type (CircularAperture, FloatingAperture, None, ...) and its MinimumRadius, MaximumRadius, ..."""
        raise SaosError(ErrorCode.ZOS_ERROR, f"{type(self).__name__} does not support surface_aperture")

    def viz_data(self, folder: Path | None, *, full: bool = True, rays: bool = True) -> dict[str, Any]:
        """saos ui 的数据：逐面全局几何、布局光线、Lens Data、交叠检查；full 时另有点列图光线和分析导出（文本写到 folder）。
        Data for saos ui: global geometry, layout rays, Lens Data, interference; with full also spot rays and analysis exports.
        rays=False 时没有布局光线（优化运行中）/ no layout rays with rays=False (during an optimisation)."""
        raise SaosError(ErrorCode.ZOS_ERROR, f"{type(self).__name__} does not support viz_data")

    def interference(self) -> dict[str, Any]:
        """结构交叠检查：面相交（crossings）、镜片穿插（overlaps）。/ Interference: crossing surfaces and interpenetrating lenses."""
        raise SaosError(ErrorCode.ZOS_ERROR, f"{type(self).__name__} does not support interference")

    def trace_check(self, rays: list[tuple[int, float, float, float, float]]) -> list[dict[str, Any]]:
        """追迹到像面，返回失败或被截断的光线：(wave, hx, hy, px, py) 及 error/vignette 码。"""
        raise SaosError(ErrorCode.ZOS_ERROR, f"{type(self).__name__} does not support trace_check")
