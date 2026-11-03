"""中立指标目录：复用测量库，统一标量、曲线与表格的结果。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from saos import kit, micro
from saos.protocol import ErrorCode, SaosError, finite


def _bad(message: str):
    raise SaosError(ErrorCode.BAD_ARGS, message)


@dataclass(frozen=True)
class Parameter:
    type: str
    default: Any = None
    minimum: float | None = None
    maximum: float | None = None
    required: bool = False

    def read(self, name: str, value: Any) -> Any:
        if value is None:
            if self.required:
                _bad(f"metric parameter {name} is required")
            return None
        if self.type in ("integer", "number"):
            valid = isinstance(value, int) if self.type == "integer" else isinstance(value, (int, float))
            if isinstance(value, bool) or not valid:
                _bad(f"metric parameter {name} must be a finite {self.type}")
            # number 在入口转成 float：超过 float 范围的 int 在这里报错，不留到测量里溢出；int 本身总是有限值，
            # 与浮点边界精确比较，不能对它调 isfinite（超大 int 会溢出）
            # numbers become floats here, so an int beyond float range fails now rather than overflowing in the measurement;
            # an int is always finite and compares exactly with float bounds
            if self.type == "number":
                try:
                    value = float(value)
                except OverflowError:
                    _bad(f"metric parameter {name} must be a finite {self.type}")
            if isinstance(value, float) and not math.isfinite(value):
                _bad(f"metric parameter {name} must be a finite {self.type}")
            if self.minimum is not None and value < self.minimum or self.maximum is not None and value > self.maximum:
                _bad(f"metric parameter {name} must be in [{self.minimum}, {self.maximum}]")
        elif self.type == "surface":
            if isinstance(value, bool) or not isinstance(value, (int, str)) or isinstance(value, str) and not value.strip():
                _bad(f"metric parameter {name} must be a surface number or comment")
        elif self.type == "boolean":
            if not isinstance(value, bool):
                _bad(f"metric parameter {name} must be boolean")
        elif self.type in ("fields", "pair"):
            rows = value if self.type == "fields" else [value]
            if not isinstance(rows, (list, tuple)) or not 1 <= len(rows) <= 64:
                _bad(f"metric parameter {name} needs 1–64 coordinate pairs")
            for row in rows:
                if not isinstance(row, (list, tuple)) or len(row) != 2:
                    _bad(f"metric parameter {name} needs coordinate pairs")
                for v in row:
                    if v is None:
                        _bad(f"metric parameter {name} needs finite coordinates")
                    Parameter("number", minimum=-1, maximum=1).read(name, v)
        elif self.type == "settings":
            if not isinstance(value, dict) or len(value) > 32 or not all(isinstance(k, str) for k in value):
                _bad(f"metric parameter {name} must be a settings mapping of at most 32 entries")
        return value


@dataclass(frozen=True)
class Metric:
    id: str
    name: str
    unit: str
    shape: str
    fn: Callable[..., dict[str, Any]]
    params: dict[str, Parameter] = field(default_factory=dict)
    scalars: tuple[str, ...] = ("value",)
    operand: str | None = None
    analysis: str | None = None
    x_axis: dict[str, str] | None = None
    y_axis: dict[str, str] | None = None
    scalar_units: dict[str, str] = field(default_factory=dict)

    def arguments(self, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict) or set(params) - self.params.keys():
            _bad(f"{self.id}: params must be a mapping; allowed: {sorted(self.params)}")
        # 有缺省值的参数显式给 null 会绕过缺省值落到测量函数里，在入口拒绝 / an explicit null would bypass the default
        nulls = sorted(k for k, v in params.items() if v is None and self.params[k].default is not None)
        if nulls:
            _bad(f"{self.id}: {', '.join(nulls)} must not be null; omit a parameter to use its default")
        return {key: p.read(key, params.get(key, p.default)) for key, p in self.params.items()}


WAVE = Parameter("integer", minimum=1, maximum=1000)
GRID = Parameter("integer", 21, 4, 257)
FIELDS = Parameter("fields", ((0.0, 0.0),))
TO = Parameter("surface")
SPOT_PARAMS = {"fields": FIELDS, "wave": WAVE, "grid": GRID, "to": TO}


def _operand(name):
    def run(ctx, wave=None):
        return {"value": kit.op(ctx, name, 0, kit.primary_wave(ctx) if wave is None else wave)}
    return run


def _thickness(ctx, first, last):
    first, last = kit.surface(ctx, first), kit.surface(ctx, last)
    if last < first:
        _bad("total_thickness needs last >= first")
    return {"value": kit.op(ctx, "TTHI", first, last)}


def _spot(ctx, **params):
    rows = kit.spot(ctx, **params)
    # 整束光未到达时仍保留点列统计，但不给可用于达标判定的数值。
    valid = all(r["valid"] and r["arrived"] == r["launched"] for r in rows)
    def worst(key):
        values = [r.get(key) for r in rows]
        return max(values) if valid and values and all(v is not None for v in values) else None
    return {"value": worst("rms_radius"), "scalars": {"rms_radius": worst("rms_radius"), "geo_radius": worst("geo_radius"),
            "pass_rate": min(r["pass_rate"] for r in rows)}, "table": {"rows": rows}}


def _focus(ctx, **params):
    rows = kit.spot(ctx, **params, refocus=True)
    valid = all(r["valid"] and r["arrived"] == r["launched"] and r.get("best_dz") is not None for r in rows)
    value = max((r["best_dz"] for r in rows), key=abs) if valid else None
    return {"value": value, "table": {"rows": rows}}


def _spot_diagram(ctx, fields, wave, grid, to):
    wave = kit.primary_wave(ctx) if wave is None else wave
    series = []
    for hx, hy in fields:
        rays = kit.trace(ctx, [(wave, hx, hy, px, py) for px, py in kit.pupil(grid)], to)
        series.append({"name": f"{hx}, {hy}", "x": [float(x) for x in rays.x[rays.ok]],
                       "y": {"spots": [float(y) for y in rays.y[rays.ok]]}, "render": "scatter",
                       "launched": len(rays.ok), "arrived": int(rays.ok.sum())})
    return {"value": None, "series": series}


def _cra(ctx, **params):
    result = kit.cra(ctx, **params)
    return {"value": result["value"] if result["coverage"]["ok"] else None,
            "scalars": {"maximum": result["value"] if result["coverage"]["ok"] else None,
                        "at": result["at"] if result["coverage"]["ok"] else None},
            "series": [{"name": "CRA", "x": [p[0] for p in result["samples"]], "y": {"angle": [p[1] for p in result["samples"]]}}],
            "coverage": result["coverage"], "details": result}


def _ray_fan(ctx, fields, wave, n, to):
    waves = [kit.primary_wave(ctx) if wave is None else wave]
    pupil = np.linspace(-1, 1, n).tolist()
    series = []
    for hx, hy in fields:
        chief = kit.trace(ctx, [(kit.primary_wave(ctx), hx, hy, 0, 0)], to)
        for w in waves:
            for name, coordinates, axis in (("tangential", [(0, p) for p in pupil], "y"),
                                             ("sagittal", [(p, 0) for p in pupil], "x")):
                traced = kit.trace(ctx, [(w, hx, hy, px, py) for px, py in coordinates], to)
                offsets = getattr(traced, axis) - getattr(chief, axis)[0]
                series.append({"name": f"{hx}, {hy}; wave {w}; {name}", "x": pupil,
                               "y": {name: [float(v) if ok and chief.ok[0] else None for v, ok in zip(offsets, traced.ok)]}})
    return {"value": None, "series": series, "reference": "primary wavelength chief ray"}


def _field_focus(ctx, direction, wave, n, grid, to):
    points = np.linspace(0, 1, n).tolist()
    fields = [(q * direction[0], q * direction[1]) for q in points]
    result = _focus(ctx, fields=fields, wave=wave, grid=grid, to=to)
    rows = result["table"]["rows"]
    return {**result, "series": [{"name": "geometric best focus", "x": points,
                                  "y": {"dz": [r.get("best_dz") if r["arrived"] == r["launched"] else None for r in rows]}}]}


def _scalar(fn):
    def run(ctx, **params):
        if "wave" in params and params["wave"] is None:
            params["wave"] = kit.primary_wave(ctx)
        return {"value": fn(ctx, **params)}
    return run


def _analysis(name):
    def run(ctx, settings):
        result = kit.analysis(ctx, name, settings)
        # 分析的部分成功不能支撑正式判定；曲线仍保留供查看。
        return {"value": None, "series": result.get("series", []), "grids": result.get("grids", []),
                "status": result.get("status", "ok"), "problems": result.get("problems", []), "details": result}
    return run


def _mtf(ctx, settings, frequency):
    result = _analysis("FftMtf")(ctx, settings)
    samples = []
    if frequency is not None and result["status"] == "ok":
        for series in result["series"]:
            if "diff" in series.get("description", "").lower():
                continue
            x = series.get("x", [])
            for y in series.get("y", {}).values():
                if (not x or len(x) != len(y) or any(finite(v) is None for v in [*x, *y])
                        or any(b <= a for a, b in zip(x, x[1:])) or not x[0] <= frequency <= x[-1]):
                    return result
                samples.append(float(np.interp(frequency, x, y)))
    result["value"] = min(samples) if samples else None
    return result


CATALOG: dict[str, Metric] = {}
for _id, _name, _unit, _op in (
    ("efl", "有效焦距", "mm", "EFFL"),
    ("working_f_number", "工作 F 数", "", "WFNO"),
    ("image_f_number", "像方 F 数", "", "ISFN"),
    ("total_track", "光学总长", "mm", "TOTR"),
    ("entrance_pupil_diameter", "入瞳直径", "mm", "EPDI"),
    ("entrance_pupil_position", "入瞳位置", "mm", "ENPP"),
    ("exit_pupil_diameter", "出瞳直径", "mm", "EXPD"),
    ("exit_pupil_position", "出瞳位置", "mm", "EXPP"),
    ("petzval", "Petzval 曲率", "1/mm", "PETZ"),
):
    CATALOG[_id] = Metric(_id, _name, _unit, "scalar", _operand(_op), {"wave": WAVE}, operand=_op)

for _metric in (
    Metric("total_thickness", "指定面区间的厚度之和", "mm", "scalar", _thickness,
           {"first": Parameter("surface", 1), "last": Parameter("surface", -2)}, operand="TTHI"),
    Metric("spot", "各视场光斑半径", "mm", "table", _spot, SPOT_PARAMS,
           ("value", "rms_radius", "geo_radius", "pass_rate"), scalar_units={"pass_rate": "%"}),
    Metric("best_focus", "几何最佳焦点偏移", "mm", "table", _focus, SPOT_PARAMS),
    Metric("spot_diagram", "点列图", "mm", "curve", _spot_diagram, SPOT_PARAMS, (),
           x_axis={"name": "x", "unit": "mm"}, y_axis={"name": "y", "unit": "mm"}),
    Metric("cra", "主光线角", "deg", "curve", _cra,
           {"direction": Parameter("pair", (0.0, 1.0)), "wave": WAVE, "n": Parameter("integer", 33, 3, 257),
            "refine": Parameter("integer", 2, 0, 4), "to": TO}, ("value", "maximum", "at"),
           x_axis={"name": "归一化视场", "unit": ""}, y_axis={"name": "主光线角", "unit": "deg"}, scalar_units={"at": ""}),
    Metric("ray_fan", "光线扇", "mm", "curve", _ray_fan,
           {"fields": FIELDS, "wave": WAVE, "n": Parameter("integer", 33, 3, 257), "to": TO}, (),
           x_axis={"name": "归一化光瞳", "unit": ""}, y_axis={"name": "相对主波长主光线的偏移", "unit": "mm"}),
    Metric("field_focus", "几何最佳焦点随视场变化", "mm", "curve", _field_focus,
           {"direction": Parameter("pair", (0.0, 1.0)), "wave": WAVE, "n": Parameter("integer", 17, 3, 64),
            "grid": GRID, "to": TO}, x_axis={"name": "归一化视场", "unit": ""}, y_axis={"name": "焦点偏移", "unit": "mm"}),
    Metric("marginal_na", "边缘光线数值孔径", "", "scalar", _scalar(micro.marginal_na),
           {"wave": WAVE, "to": TO, "rays": Parameter("integer", 32, 4, 1024)}, operand="INDX"),
    Metric("collection_fraction", "几何收集比例", "", "scalar", _scalar(micro.collection_fraction),
           {"fields": FIELDS, "wave": WAVE, "n": Parameter("integer", 61, 4, 257)}),
    Metric("min_gap", "相邻面最小间隙", "mm", "scalar", _scalar(micro.min_gap),
           {"front": Parameter("surface", required=True), "radius": Parameter("number", minimum=0),
            "rings": Parameter("integer", 17, 2, 257), "spokes": Parameter("integer", 32, 4, 1024)}, operand="SSAG"),
    Metric("mtf", "FFT MTF", "", "curve", _mtf,
           {"settings": Parameter("settings", {}), "frequency": Parameter("number", minimum=0)},
           analysis="FftMtf", x_axis={"name": "空间频率", "unit": "cycles/mm"}, y_axis={"name": "MTF", "unit": ""}),
):
    CATALOG[_metric.id] = _metric


DEFINITIONS = {
    "total_thickness": "TTHI：含 first、last 两端的面厚度之和；默认第 1 面至像面之前一面。",
    "spot": "主波长、指定归一化视场、相对质心的最坏 RMS/GEO 半径；任一采样光线未到达则半径标量无效；pass_rate 为最小到达百分比。",
    "best_focus": "均匀像方介质内使几何 RMS 最小的局部 z 偏移；多视场返回绝对值最大的有符号偏移。",
    "field_focus": "几何最佳焦点偏移随归一化视场的曲线；标量为绝对值最大的有符号偏移，不含衍射或子午/弧矢焦面分解。",
    "spot_diagram": "指定波长的像面落点，保留绝对坐标及到达计数；仅显示，不用于判定。",
    "ray_fan": "子午扇 y、弧矢扇 x 相对主波长主光线的偏移；丢失光线留空；仅显示。",
    "cra": "沿归一化视场方向加密采样的主光线角最大值；coverage 不完整时判定标量无效；缺省覆盖全部波长。",
    "marginal_na": "指定面前介质的 n·sin(u)，取轴上光瞳边缘一圈光线的最小值；任一光线丢失则无效；局部 z 须沿光轴。",
    "collection_fraction": "像方方向网格积分立体角除以 4π，各视场取平均；不含菲涅耳损失和吸收。",
    "min_gap": "front 与后一面在采样半径内的最小矢高间隙，复用 micro.min_gap 的孔径约定。",
    "mtf": "FFT MTF；frequency 给定时在线性插值后取全部非衍射极限曲线的最小值；频率越界或部分失败则标量无效。",
}


def _description(metric: Metric) -> str:
    return DEFINITIONS.get(metric.id, f"后端 {metric.operand} 操作数；wave 缺省为主波长，操作数不使用的参数由后端忽略。")


def definition(metric_id: str) -> Metric:
    if not isinstance(metric_id, str) or metric_id not in CATALOG:
        _bad(f"unknown metric {metric_id!r}; use saos metrics list")
    return CATALOG[metric_id]


def validate(spec: Any, *, judging: bool = False) -> tuple[Metric, dict[str, Any]]:
    if not isinstance(spec, dict) or set(spec) - {"id", "params", "pick"}:
        _bad("metric needs {id, params?, pick?}")
    metric = definition(spec.get("id"))
    params = metric.arguments(spec.get("params", {}))
    pick = spec.get("pick")
    if pick is not None and (not isinstance(pick, str) or not pick or pick not in metric.scalars):
        _bad(f"{metric.id}: pick must be one of {metric.scalars}")
    if judging and not metric.scalars:
        _bad(f"{metric.id} is display-only; it has no judging scalar")
    if judging and metric.id == "mtf" and params["frequency"] is None:
        _bad("mtf judging needs params.frequency in cycles/mm")
    return metric, params


def measure(ctx, metric_id: str, **params) -> dict[str, Any]:
    metric, args = validate({"id": metric_id, "params": params})
    if metric.unit in ("mm", "1/mm") or metric.id == "mtf":
        units = ctx.backend.system_info().get("lens_units")
        if units != "Millimeters":
            raise SaosError(ErrorCode.BACKEND_UNSUPPORTED,
                            f"{metric.id} currently requires Millimeters lens units, got {units!r}")
    try:
        result = metric.fn(ctx, **args)
    except micro.DesignFailure as exc:
        result = {"value": None, "design_failure": True, "reason": str(exc)}
    result.setdefault("value", None)
    result["value"] = finite(result["value"])
    result["scalars"] = {k: finite(v) for k, v in result.get("scalars", {}).items()}
    result["scalars"].setdefault("value", result["value"])
    return {"id": metric.id, "name": metric.name, "unit": metric.unit, "shape": metric.shape,
            "description": _description(metric),
            "x_axis": metric.x_axis, "y_axis": metric.y_axis, "params": args,
            "scalar_units": {key: metric.scalar_units.get(key, metric.unit) for key in metric.scalars}, **result}


def scalar(result: dict[str, Any], spec: dict[str, Any]) -> float | None:
    return finite(result["scalars"].get(spec.get("pick", "value")))


def picked(spec: dict[str, Any]) -> dict[str, str]:
    """条目的 value 是 pick 选中的标量，单位跟着它走，不能沿用指标的基本单位（spot 的 pass_rate 是 %，不是 mm）。
    An entry's value is the picked scalar, so its unit is that scalar's unit, not the metric's base unit."""
    metric = definition(spec.get("id"))
    pick = spec.get("pick") or "value"
    return {"pick": pick, "unit": metric.scalar_units.get(pick, metric.unit)}


def catalog(ctx) -> list[dict[str, Any]]:
    support = getattr(ctx.backend, "metric_support", None)
    return [{"id": m.id, "name": m.name, "unit": m.unit, "shape": m.shape,
             "description": _description(m),
             "scalars": list(m.scalars), "x_axis": m.x_axis, "y_axis": m.y_axis,
             "scalar_units": {key: m.scalar_units.get(key, m.unit) for key in m.scalars},
             "params": {k: vars(v) for k, v in m.params.items()},
             "availability": support(operand=m.operand, analysis=m.analysis) if support else "unknown"}
            for m in CATALOG.values()]


class MetricMethods:
    """ctx.metric / ctx.metrics：脚本与 worker 上下文的目录入口。放在本模块而非 kit.KitMethods，kit 就不必反向导入本模块；
    rtc 副本（fast.FastLens）只服务射线量循环，不带目录。
    The catalog on script and worker contexts. It lives here, not in kit.KitMethods, so kit never imports this module;
    the rtc copy serves ray loops only and has no catalog."""

    def metric(self, metric_id, **params):
        return measure(self, metric_id, **params)

    def metrics(self):
        return catalog(self)
