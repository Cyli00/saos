"""热结构的 OpticStudio 真值：变体定义和读写，录真值的 tests/test_zemax_thermal.py 和离线的 tests/rtc/test_thermal_truth.py 共用。

每个变体是 tests/fixtures/cases 里一个案例的副本加一处改动，用 Make Thermal 变成多结构，服务 src/rtc/thermal.py 模块说明里的某条规则。
真值在 tests/fixtures/cases/thermal.json，一个变体一行。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

CASES = Path(__file__).parent / "fixtures" / "cases"
TRUTH_FILE = CASES / "thermal.json"
TCE = 23.6


def base(case: str) -> dict[str, Any]:
    return json.loads((CASES / f"{case}.json").read_text(encoding="utf-8"))


def _asphere(p):
    s = p["surfaces"][0]
    s["type"], s["conic"] = "EvenAspheric", -0.5
    s["params"] = {"par2": -3e-5, "par3": 2e-7, "par4": -1e-9}


def _model_glass(p):
    p["surfaces"][0]["material"] = {"model": {"nd": 1.6, "vd": 45.0}}


def _air_after_air(p):
    p["surfaces"][2]["thickness"] = 5.0
    p["surfaces"].append({"radius": 150.0, "thickness": 10.0, "material": "", "semi_diameter": 6.0})
    p["surfaces"][2].pop("solves", None)
    p["surfaces"][3]["solves"] = {"thickness": {"type": "marginal_ray_height", "height": 0}}


def _curved_stop(p):
    p["surfaces"][5]["radius"] = 100.0


def _air_after_mirror(p):
    """空气中的反射镜之后接一个弯曲的空气面。"""
    p["surfaces"].insert(1, {"radius": -3000.0, "thickness": -1.0, "material": "", "semi_diameter": 12.0})
    p["surfaces"][0]["thickness"] -= -1.0


def _finite_object(p):
    p["object"]["thickness"] = 500.0
    p["aperture"] = {"type": "object_na", "value": 0.01}
    p["fields"] = {"type": "object_height", "y": [0, 3, 5]}


def _curved_image(p):
    p["image"] = {"type": "standard", "radius": -200.0}


def _set_cell(system, kind: str, surface: int, config: int, value: float) -> None:
    for j in range(1, int(system.MCE.NumberOfOperands) + 1):
        row = system.MCE.GetOperandAt(j)
        if str(row.TypeName) == kind and int(row.Param1) == surface:
            row.GetOperandCell(config).DoubleValue = float(value)
            return
    raise KeyError((kind, surface))


def _nominal_edits(backend) -> None:
    """改结构 1 的半径、厚度、半口径和结构 2 的温度：跟随格要跟着变。"""
    s = backend.system
    _set_cell(s, "CRVT", 1, 1, 1.0 / 50.0)
    _set_cell(s, "THIC", 2, 1, 7.0)
    _set_cell(s, "CRVT", 8, 1, -1.0 / 60.0)
    _set_cell(s, "SDIA", 2, 1, 5.5)
    _set_cell(s, "TEMP", 0, 2, 0.0)


def _mech_edits(backend) -> None:
    """半口径和机械半口径的三种关系：小于、等于、大于。"""
    s = backend.system
    _set_cell(s, "MCSD", 2, 1, 9.0)
    _set_cell(s, "MCSD", 3, 1, 5.2038694609)


def _pressure(backend) -> None:
    _set_cell(backend.system, "PRES", 0, 2, 0.5)


def _surface_tce(backend) -> None:
    """Make Thermal 之后逐面改 TCE：间隔只看前一面的。"""
    for i in range(1, int(backend.system.LDE.NumberOfSurfaces) - 1):
        backend.system.LDE.GetSurfaceAt(i).TCE = 10.0 * i


@dataclass(frozen=True)
class Variant:
    name: str
    case: str
    temperatures: tuple[float, ...]
    serves: str
    edit: Callable[[dict[str, Any]], None] | None = None
    after: Callable[[Any], None] | None = None
    #: 离线比对追迹的结构；有些变体只服务单元格
    rays: bool = True

    def prescription(self) -> dict[str, Any]:
        p = base(self.case)
        if self.edit is not None:
            self.edit(p)
        p.setdefault("environment", {})["adjust_index"] = True
        return p


ALL = [
    Variant("seq_2", "sequential", (-40.0,), "求解码（2 结构为 4）、胶合、最后一个间隔"),
    Variant("seq_3", "sequential", (-40.0, 65.0), "求解码（3 结构为 5）"),
    Variant("seq_4", "sequential", (-40.0, 0.0, 65.0), "求解码（4 结构为 6）"),
    Variant("seq_12", "sequential", (-40.0, -30.0, -20.0, -10.0, 0.0, 10.0, 30.0, 40.0, 50.0, 60.0, 70.0), "求解码（12 结构为 14）", rays=False),
    Variant("iris_6", "iris", (-30.0, -10.0, 0.0, 40.0, 70.0), "空气光阑、面孔径、Float By Stop Size"),
    Variant("iris_curved_stop", "iris", (-40.0, 65.0), "弯曲的空气面（前一面也是空气）不缩放", _curved_stop),
    Variant("iris_edits", "iris", (-40.0, 65.0), "跟随格实时跟随结构 1 和温度", after=_nominal_edits),
    Variant("iris_surface_tce", "iris", (-40.0, 65.0), "间隔用前一面的 TCED", after=_surface_tce),
    Variant("seq_asphere", "sequential", (-40.0, 65.0), "Par 系数按 (1+a)^-(2n-1)，圆锥不变", _asphere),
    Variant("seq_model", "sequential", (-40.0, 65.0), "模型玻璃的 α 取 TCED", _model_glass),
    Variant("seq_air_after_air", "sequential", (-40.0, 65.0), "前一面是空气的面只有 THIC 行", _air_after_air),
    Variant("seq_mech", "sequential", (-40.0, 65.0), "接触半径的两个分支（半口径 < 机械半口径与否）", after=_mech_edits),
    Variant("seq_pressure", "sequential", (-40.0, 65.0), "气压不改几何，改折射率", after=_pressure),
    Variant("seq_curved_image", "sequential", (-40.0, 65.0), "弯曲的像面", _curved_image),
    Variant("seq_finite_object", "sequential", (-40.0, 65.0), "有限物距的物面厚度", _finite_object),
    Variant("reflective_3", "reflective", (-40.0, 65.0), "空气中和玻璃内的反射镜、负厚度"),
    Variant("reflective_air_after_mirror", "reflective", (-40.0, 65.0), "反射镜之后的空气面", _air_after_mirror),
]


@lru_cache(maxsize=1)
def _all_truth() -> dict[str, Any]:
    return json.loads(TRUTH_FILE.read_text(encoding="utf-8")) if TRUTH_FILE.exists() else {}


def truth(name: str) -> dict[str, Any]:
    try:
        return _all_truth()[name]
    except KeyError as exc:
        raise KeyError(f"缺少 {name} 的 OpticStudio 真值；请运行 SAOS_RUN_ZEMAX=1 SAOS_CASE_RECORD=1 uv run pytest tests/test_zemax_thermal.py -s -k record") from exc


def save_truth(name: str, data: dict[str, Any]) -> None:
    records = {**_all_truth(), name: data}
    lines = [f"{json.dumps(v.name)}:{json.dumps(records[v.name], separators=(',', ':'))}" for v in ALL if v.name in records]
    TRUTH_FILE.write_text("{\n" + ",\n".join(lines) + "\n}\n", encoding="utf-8")
    _all_truth.cache_clear()
