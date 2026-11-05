"""iris、reflective 的变体定义，录真值的 tests/test_zemax_cases.py 和离线的 tests/rtc/test_case_truth.py 共用。

每个变体是基础处方的副本加一处改动，服务 docs/plans/rtc-iris-mirror.md 第 2.3 节的某一项。
基础版的 .zmx 在 tests/fixtures/cases/，变体的 .zmx 和所有读数在 truth.json，一个变体一行。
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from rtc import zmx

CASES = Path(__file__).parent / "fixtures" / "cases"
TRUTH_FILE = CASES / "truth.json"

# 用于手写渐晕因子：偏心、压缩和旋转都不为零，方向错了结果就变
HANDWRITTEN_VIGNETTING = {"vdx": [0.06, 0.04, 0.0, -0.05], "vdy": [0.0, -0.03, 0.02, 0.03],
                          "vcx": [0.10, 0.12, 0.15, 0.08], "vcy": [0.08, 0.10, 0.12, 0.14],
                          "van": [0.0, 3.0, 6.0, -4.0]}


@lru_cache(maxsize=1)
def _all_truth() -> dict[str, Any]:
    return json.loads(TRUTH_FILE.read_text(encoding="utf-8")) if TRUTH_FILE.exists() else {}


def truth(name: str) -> dict[str, Any]:
    try:
        return _all_truth()[name]
    except KeyError as exc:
        raise KeyError(f"缺少 {name} 的 OpticStudio 真值；请运行 "
                       f"SAOS_RUN_ZEMAX=1 SAOS_CASE_RECORD=1 uv run pytest tests/test_zemax_cases.py -s -k {name}") from exc


def save_truth(name: str, data: dict[str, Any]) -> None:
    records = {**_all_truth(), name: data}
    lines = [f"{json.dumps(v.name)}:{json.dumps(records[v.name], separators=(',', ':'))}" for v in ALL if v.name in records]
    TRUTH_FILE.write_text("{\n" + ",\n".join(lines) + "\n}\n", encoding="utf-8")
    _all_truth.cache_clear()


def pupil_grid(n: int) -> list[tuple[float, float]]:
    """单位圆内的 n×n 方格光瞳点。"""
    if n < 2:
        return [(0.0, 0.0)]
    axis = [-1.0 + 2.0 * k / (n - 1) for k in range(n)]
    return [(x, y) for y in axis for x in axis if x * x + y * y <= 1.0 + 1e-12]


def base(case: str) -> dict[str, Any]:
    return json.loads((CASES / f"{case}.json").read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Variant:
    name: str
    case: str
    serves: str
    edit: Callable[[dict[str, Any]], None] | None = None
    #: 建好镜头后在 OpticStudio 上再做的事，比如 Set Vignetting
    after: Callable[[Any], None] | None = None
    #: "all" 所有波长，"primary" 只录主波长，"none" 不录光线
    waves: str = "primary"
    #: 只录这些视场（下标），None 表示全部
    fields: tuple[int, ...] | None = None
    #: 点名的光线 (hx, hy, px, py)，归入常规比对
    named: tuple[tuple[float, float, float, float], ...] = ()
    #: 表外视场 (hx, hy)，真值单独存放，R6 才比
    off_table: tuple[tuple[float, float], ...] = ()
    #: 方位角扫描的光瞳半径，用来定蜘蛛臂的方向
    ring: float | None = None
    #: 录 OPDC、OPDX 和 RMS 波前
    opd: bool = False
    #: 这条路径现在是否已放行，离线比对只跑放行的
    admitted: bool = False

    def prescription(self) -> dict[str, Any]:
        raw = copy.deepcopy(base(self.case))
        if self.edit is not None:
            self.edit(raw)
        return raw

    @property
    def is_base(self) -> bool:
        return self.name == self.case

    def lens(self) -> zmx.Lens:
        return zmx.parse(CASES / f"{self.case}.zmx") if self.is_base else zmx.parse_text(truth(self.name)["zmx"])


def _real(raw):
    raw["ray_aiming"] = "real"


def _iris_stop_front(raw):
    _real(raw)
    # 光阑前移后，矩形孔离光阑太远，离轴光束全被它挡住，这个变体只看瞄准，去掉它
    raw["surfaces"][6].pop("aperture")
    raw["surfaces"][5].pop("stop")
    raw["surfaces"][5].pop("semi_diameter")
    raw["surfaces"].insert(0, {"radius": "inf", "thickness": 4, "stop": True, "semi_diameter": 2.5,
                               "comment": "stop in front of the first lens"})


def _iris_semi(value):
    def edit(raw):
        raw["surfaces"][5]["semi_diameter"] = value
    return edit


def _iris_no_apertures(raw):
    raw["surfaces"][0].pop("aperture")
    raw["surfaces"][6].pop("aperture")


def _iris_vignetting(raw):
    raw["fields"]["vignetting"] = copy.deepcopy(HANDWRITTEN_VIGNETTING)


def _iris_vignetting_only(raw):
    _iris_no_apertures(raw)
    _iris_vignetting(raw)


def _iris_added_aperture(aperture):
    """在光阑和矩形孔之间的空气里加一个平面，带要验证的孔径。"""
    def edit(raw):
        raw["surfaces"][5]["thickness"] = 1.5
        raw["surfaces"].insert(6, {"radius": "inf", "thickness": 1.5, "aperture": dict(aperture), "comment": "aperture under test"})
    return edit


def _floating_stop(zos):
    # 处方写不了浮动孔径，只能在 OpticStudio 上设
    row = zos.system.LDE.GetSurfaceAt(6)
    kind = zos.zosapi().Editors.LDE.SurfaceApertureTypes.FloatingAperture
    if not row.ApertureData.ChangeApertureTypeSettings(row.ApertureData.CreateApertureTypeSettings(kind)):
        raise RuntimeError("OpticStudio refused the floating aperture")


def _set_vignetting(zos):
    zos.system.SystemData.Fields.SetVignetting()


def _semi_setting(fast=None, margin=None, percent=None):
    def after(zos):
        aperture = zos.system.SystemData.Aperture
        if fast is not None:
            aperture.FastSemiDiameters = fast
        if margin is not None:
            aperture.SemiDiameterMargin = margin
        if percent is not None:
            aperture.SemiDiameterMarginPct = percent
    return after


def _reflective_no_obscuration(raw):
    raw["surfaces"][0].pop("aperture")


def _reflective_one_mirror(raw):
    raw["surfaces"] = raw["surfaces"][:1]
    raw["surfaces"][0]["thickness"] = -400


def _reflective_stop_on_secondary(raw):
    raw["surfaces"][0].pop("stop")
    raw["surfaces"][2]["stop"] = True


def _reflective_even_asphere(raw):
    raw["surfaces"][0].update(type="EvenAspheric", params={"par2": 1e-11})


def _reflective_biconic(raw):
    raw["surfaces"][2].update(type="Biconic", params={"par1": -420.0, "par2": 0.0})


def _reflective_toroidal(raw):
    raw["surfaces"][2].update(type="Toroidal", params={"par1": 0.0})


def _reflective_paraxial_after(raw):
    raw["surfaces"][3]["thickness"] = 200.0
    raw["surfaces"].append({"type": "Paraxial", "thickness": 100.0, "focal_length": 2000.0, "opd_mode": 0})


def _reflective_one_mirror_paraxial(raw):
    _reflective_one_mirror(raw)
    raw["surfaces"][0]["thickness"] = -200.0
    raw["surfaces"].append({"type": "Paraxial", "thickness": -200.0, "focal_length": 2000.0, "opd_mode": 0})


_AXIAL_AND_EDGE = (0, 2)
# 前两点排除插值；(0.26, 0.72) 和 (1, 0) 上欧氏与曼哈顿距离的最近行不同，用来定距离度量
OFF_TABLE = ((0.0, 0.7), (0.5, 0.0), (0.26, 0.72), (1.0, 0.0))
_APERTURES = {
    "rect_obscuration": {"type": "rectangular_obscuration", "x_half": 0.8, "y_half": 0.6},
    "ellipse": {"type": "elliptical", "x_half": 2.4, "y_half": 1.8},
    "ellipse_obscuration": {"type": "elliptical_obscuration", "x_half": 0.9, "y_half": 0.6},
    "spider": {"type": "spider", "arms": 4, "width": 0.5},
    "circle_min": {"type": "circular", "min": 0.8, "max": 2.6},
}

ALL: list[Variant] = [
    # ---- 基础版
    Variant("iris", "iris", "A1、A2、渐晕码", waves="all", admitted=True),
    Variant("iris_floating", "iris", "浮动孔径（类型码 10）", after=_floating_stop, admitted=True),
    Variant("reflective", "reflective", "B、遮挡、穿过主镜孔、R8 的环形光瞳", waves="all", opd=True, admitted=True),
    # ---- R1
    Variant("iris_real", "iris", "R1", _real),
    Variant("iris_stop_front", "iris", "R1", _iris_stop_front),
    # ---- R2：改半口径后系统孔径要跟着变
    Variant("iris_semi_2", "iris", "R2", _iris_semi(2.0), admitted=True),
    # ---- R5：把挡光、渐晕因子、取样、余量四个因素分开
    Variant("iris_no_apertures", "iris", "R5", _iris_no_apertures, admitted=True),
    Variant("iris_vignetting_only", "iris", "R5、R6", _iris_vignetting_only, waves="none"),
    Variant("iris_fast_on", "iris", "R5", after=_semi_setting(fast=True), waves="none"),
    Variant("iris_fast_off", "iris", "R5", after=_semi_setting(fast=False), waves="none"),
    Variant("iris_margin_percent", "iris", "R5", after=_semi_setting(percent=10.0), waves="none"),
    Variant("iris_margin_mm", "iris", "R5", after=_semi_setting(margin=0.5), waves="none"),
    Variant("iris_margin_both", "iris", "R5", after=_semi_setting(margin=0.5, percent=10.0), waves="none"),
    # ---- R6
    Variant("iris_set_vignetting", "iris", "R6", after=_set_vignetting, off_table=OFF_TABLE, admitted=True),
    Variant("iris_vignetting", "iris", "R6", _iris_vignetting, off_table=OFF_TABLE, admitted=True),
    # ---- R7
    Variant("reflective_real", "reflective", "R7", _real),
    Variant("reflective_stop_on_secondary", "reflective", "R7", _reflective_stop_on_secondary, admitted=True),
    Variant("reflective_stop_on_secondary_real", "reflective", "R7",
            lambda raw: (_reflective_stop_on_secondary(raw), _real(raw))),
    Variant("reflective_even_asphere", "reflective", "R7", _reflective_even_asphere),
    Variant("reflective_biconic", "reflective", "R7", _reflective_biconic),
    Variant("reflective_toroidal", "reflective", "R7", _reflective_toroidal),
    Variant("reflective_paraxial_after", "reflective", "R7", _reflective_paraxial_after),
    Variant("reflective_one_mirror_paraxial", "reflective", "R7", _reflective_one_mirror_paraxial),
    # ---- 奇数次反射、R8
    Variant("reflective_one_mirror", "reflective", "奇数次反射、R8", _reflective_one_mirror, opd=True, admitted=True),
    Variant("reflective_no_obscuration", "reflective", "R8", _reflective_no_obscuration, opd=True, admitted=True),
    Variant("sequential_opd", "sequential", "R8：第 1 面弯曲的折射系统", opd=True, admitted=True),
    # ---- R3：每种孔径有无偏心各一版
    *[Variant(f"iris_{key}", "iris", "R3", _iris_added_aperture(ap), fields=_AXIAL_AND_EDGE, ring=0.7 if key == "spider" else None)
      for key, ap in _APERTURES.items()],
    *[Variant(f"iris_{key}_decentred", "iris", "R3", _iris_added_aperture({**ap, "x_decenter": 0.3, "y_decenter": -0.2}),
              fields=_AXIAL_AND_EDGE, ring=0.7 if key == "spider" else None)
      for key, ap in _APERTURES.items()],
]
BY_NAME = {v.name: v for v in ALL}
ADMITTED = [v.name for v in ALL if v.admitted]
