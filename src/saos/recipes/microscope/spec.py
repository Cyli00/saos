"""光学规格（YAML）的读取、补全和冻结，口径见 docs/optical-spec-and-acceptance.md。

规格中的每个值既可以直接写，也可以写成 {value: ..., source: user|assumption, basis: ...}。
这里只取 value；source、basis 原样保留在规格文件里，供设计计划使用。

阈值和归一化尺度只由规格值计算（标称倍率、NA、OFN），冻结后不随设计变化。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from saos.protocol import ErrorCode, SaosError

SPEC_VERSION = 1
LAMBDA_E_UM = 0.54607
WAVELENGTHS_UM = (0.47999, 0.54607, 0.64385)  # F′、e、C′（ISO 19012-2）
BEREK_OMEGA = 0.0014
FIELD_POINTS = (0.0, 0.3, 0.5, 0.7, 0.85, 1.0)
PFR_GRID = tuple(j / 20 for j in range(21))
MARECHAL_WFE = 1 / 14
REFERENCE_TUBE_MM = 200.0  # 参考筒镜默认焦距


def _value(raw: Any) -> Any:
    if isinstance(raw, dict) and "value" in raw:
        return raw["value"]
    return raw


def _need(data: dict[str, Any], key: str, kind: type | tuple[type, ...]) -> Any:
    value = _value(data.get(key))
    if value is None:
        raise SaosError(ErrorCode.BAD_ARGS, f"optical spec is missing {key}")
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        value = float(value)
    if not isinstance(value, kind):
        raise SaosError(ErrorCode.BAD_ARGS, f"optical spec field {key} has the wrong type: {value!r}")
    return value


def _opt(data: dict[str, Any] | None, key: str, default: Any) -> Any:
    if not data:
        return default
    value = _value(data.get(key, default))
    return default if value is None else value


@dataclass(frozen=True)
class Item:
    """一个结构界限或性能指标的判定口径。kind：upper、lower、count。"""

    id: str
    category: str  # constraint / target
    kind: str
    threshold: float
    scale: float
    unit: str
    params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in ("upper", "lower", "count"):
            raise SaosError(ErrorCode.BAD_ARGS, f"{self.id}: unknown check kind {self.kind}")
        if not math.isfinite(self.threshold):
            raise SaosError(ErrorCode.BAD_ARGS, f"{self.id}: threshold must be finite, got {self.threshold}")
        if not (math.isfinite(self.scale) and self.scale > 0):
            raise SaosError(ErrorCode.BAD_ARGS, f"{self.id}: scale must be positive and finite, got {self.scale}")

    def valid_value(self, value: Any) -> bool:
        """测量值能否用于判定：必须是有限实数。唯一的例外是显式标记 inf_means_absent 的项
        （空气间隔），+inf 表示物镜中没有空气间隔。"""
        if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value):
            return False
        return math.isfinite(value) or (value == math.inf and self.kind == "lower" and bool(self.params.get("inf_means_absent")))

    def gap(self, value: float) -> float:
        if not self.valid_value(value):
            raise ValueError(f"{self.id}: invalid measurement {value!r}")
        if self.kind == "count":
            return max(0.0, value - self.threshold)
        if self.kind == "upper":
            return max(0.0, value - self.threshold) / self.scale
        if self.kind == "lower":
            return max(0.0, self.threshold - value) / self.scale
        raise AssertionError(self.kind)


@dataclass(frozen=True)
class Spec:
    raw: dict[str, Any]
    correction_class: str
    magnification: float
    na: float
    tube_lens_focal_length: float
    objective_field_number: float
    receiver: str
    pixel_size: float | None
    wavelengths_um: tuple[float, ...]
    wavelength_weights: tuple[float, ...]
    cover_glass: dict[str, float] | None
    glass_catalogs: tuple[str, ...]
    glass_allowed_status: tuple[str, ...]
    items: tuple[Item, ...]
    glass_allow_model: bool = False

    # ---- 派生量 ----

    @property
    def objective_focal_length(self) -> float:
        return self.tube_lens_focal_length / self.magnification

    @property
    def object_field_radius(self) -> float:
        return self.objective_field_number / (2 * self.magnification)

    @property
    def delta_ob_um(self) -> float:
        return LAMBDA_E_UM / (2 * self.na**2)

    @property
    def berek_image_um(self) -> float:
        m = self.magnification
        return m**2 * (BEREK_OMEGA * 250000 / (self.na * 10 * m) + LAMBDA_E_UM / (2 * self.na**2))

    @property
    def airy_radius_image_um(self) -> float:
        return 0.61 * LAMBDA_E_UM * self.magnification / self.na

    def item(self, item_id: str) -> Item | None:
        return next((i for i in self.items if i.id == item_id), None)

    def field_samples(self) -> list[float]:
        """验收模型需要定义的全部归一化视场点（相对 OFN/2）。"""
        points = set(PFR_GRID) | set(FIELD_POINTS)
        for item in self.items:
            if "range" in item.params:
                points |= set(sample_points(item.params["range"]))
        return sorted(points)

    def derived(self) -> dict[str, float]:
        return {
            "objective_focal_length_mm": self.objective_focal_length,
            "exit_pupil_diameter_mm": 2 * self.na * self.objective_focal_length,
            "object_field_diameter_mm": 2 * self.object_field_radius,
            "delta_ob_um": self.delta_ob_um,
            "berek_image_um": self.berek_image_um,
            "airy_radius_image_um": self.airy_radius_image_um,
        }


def sample_points(r_up: float) -> list[float]:
    """视场采样规则：固定点中不超过 r_up 的各点，再加上 r_up 本身。"""
    return sorted({p for p in FIELD_POINTS if p <= r_up + 1e-12} | {round(r_up, 6)})


def load_spec(path: Path) -> Spec:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SaosError(ErrorCode.BAD_ARGS, f"cannot read optical spec {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SaosError(ErrorCode.BAD_ARGS, "optical spec must be a YAML mapping")
    return parse_spec(data)


def parse_spec(data: dict[str, Any]) -> Spec:
    if _value(data.get("spec_version")) != SPEC_VERSION:
        raise SaosError(ErrorCode.BAD_ARGS, f"spec_version must be {SPEC_VERSION}")
    cls = _need(data, "correction_class", str)
    if cls not in ("achromat", "plan_achromat"):
        raise SaosError(ErrorCode.BAD_ARGS, f"only achromat and plan_achromat are supported, got {cls}")
    m = _need(data, "magnification", float)
    na = _need(data, "na", float)
    if not 0 < na <= 0.65:
        raise SaosError(ErrorCode.BAD_ARGS, f"only dry objectives with NA ≤ 0.65 are supported, got {na}")
    f_tl = float(_opt(data, "tube_lens_focal_length", REFERENCE_TUBE_MM))
    ofn = float(_opt(data, "objective_field_number", 22.0 if cls == "plan_achromat" else 18.0))
    for name, value in (("magnification", m), ("tube_lens_focal_length", f_tl), ("objective_field_number", ofn)):
        if not (math.isfinite(value) and value > 0):
            raise SaosError(ErrorCode.BAD_ARGS, f"{name} must be positive and finite, got {value}")
    receiver = _opt(data, "receiver", "visual")
    pixel = _opt(data, "pixel_size", None)
    if receiver not in ("visual", "camera"):
        raise SaosError(ErrorCode.BAD_ARGS, f"receiver must be visual or camera, got {receiver}")
    if receiver == "camera" and not pixel:
        raise SaosError(ErrorCode.BAD_ARGS, "a camera receiver needs pixel_size")

    waves = data.get("wavelengths") or {}
    um = tuple(float(x) for x in _opt(waves, "um", WAVELENGTHS_UM))
    weights = tuple(float(x) for x in _opt(waves, "weights", [1.0] * len(um)))
    if um != WAVELENGTHS_UM:
        raise SaosError(ErrorCode.BAD_ARGS, "acceptance uses the F′, e and C′ lines only")

    cover = _value(data.get("cover_glass"))
    if cover is not None:
        cover = {k: float(_value(cover[k])) for k in ("thickness", "nd", "vd")}

    limits = data.get("limits") or {}
    glass = _value(limits.get("glass")) or {}
    catalogs = tuple(str(c).upper() for c in _opt(glass, "catalogs", ["CDGM"]))
    allowed = tuple(_opt(glass, "allowed_status", ["Standard", "Preferred"]))
    # 模型玻璃默认不允许；只在原始资料的折射率精度不足以对应具体牌号时，由基准规格显式打开
    allow_model = bool(_opt(glass, "allow_model", False))

    items: list[Item] = []
    c = items.append
    tol = float(_opt(limits, "magnification_tolerance", 0.05))
    c(Item("magnification", "constraint", "upper", tol, tol, "ratio"))
    wd_min = _opt(limits, "working_distance_min", None)
    if wd_min is None:
        raise SaosError(ErrorCode.BAD_ARGS, "optical spec is missing limits.working_distance_min")
    c(Item("working_distance", "constraint", "lower", float(wd_min), float(wd_min), "mm"))
    length = float(_opt(limits, "optical_length_max", 45.0))
    c(Item("optical_length", "constraint", "upper", length, length, "mm"))
    diameter = float(_opt(limits, "max_clear_diameter", 16.0))
    c(Item("clear_diameter", "constraint", "upper", diameter, diameter, "mm"))
    c(Item("glass_status", "constraint", "count", 0.0, 1.0, "items"))
    c(Item("surface_types", "constraint", "count", 0.0, 1.0, "items"))
    ct = float(_opt(limits, "min_center_thickness_glass", 0.5))
    c(Item("center_thickness_glass", "constraint", "lower", ct, ct, "mm"))
    et = float(_opt(limits, "min_edge_thickness_glass", 0.3))
    c(Item("edge_thickness_glass", "constraint", "lower", et, et, "mm"))
    gap = limits.get("min_air_gap") or {"value": 0.0, "scale": 0.01}
    gap_value = float(_value(gap) if not isinstance(gap, dict) else gap.get("value", 0.0))
    gap_scale = float(gap.get("scale", gap_value) if isinstance(gap, dict) else gap_value)
    if gap_scale <= 0:
        raise SaosError(ErrorCode.BAD_ARGS, "min_air_gap of 0 needs a scale > 0")
    c(Item("air_gap", "constraint", "lower", gap_value, gap_scale, "mm", {"inf_means_absent": True}))
    max_el = _opt(limits, "max_elements", None)
    if max_el is not None:
        c(Item("elements", "constraint", "count", float(max_el), 1.0, "elements"))

    targets = data.get("targets") or {}
    delta_ob = LAMBDA_E_UM / (2 * na**2)
    wfe = float(_opt(targets, "axial_rms_wfe", MARECHAL_WFE))
    c(Item("axial_rms_wfe", "target", "upper", wfe, wfe, "λe"))
    c(Item("axial_color", "target", "upper", 2 * delta_ob, 2 * delta_ob, "µm object side"))
    osc = float(_opt(targets, "osc", 0.0025))
    c(Item("osc", "target", "upper", osc, osc, "—"))

    plan = cls == "plan_achromat"
    if plan:
        c(Item("plan_field", "target", "upper", 0.0, 1.0, "× δ′", {"ratio": 0.85}))
    fw = _value(targets.get("field_rms_wfe"))
    if plan or fw is not None:
        fw = fw or {}
        r_up = float(_opt(fw, "range", 0.85 if plan else None) or 0)
        if r_up <= 0:
            raise SaosError(ErrorCode.BAD_ARGS, "an achromat with field RMS WFE needs a range")
        thr = float(_opt(fw, "threshold", MARECHAL_WFE))
        c(Item("field_rms_wfe", "target", "upper", thr, thr, "λe", {"range": r_up, "remove_defocus_astig": plan}))
    lc = _value(targets.get("lateral_color"))
    if plan or lc is not None:
        lc = lc or {}
        airy = 0.61 * LAMBDA_E_UM * m / na
        thr = float(_opt(lc, "threshold_um", airy))
        c(Item("lateral_color", "target", "upper", thr, thr, "µm image", {"range": float(_opt(lc, "range", 1.0))}))
    mtf = _value(targets.get("mtf"))
    if receiver == "camera" or mtf is not None:
        mtf = mtf or {}
        freq = float(_opt(mtf, "frequency", 500.0 / float(pixel) if pixel else 0))
        if _opt(mtf, "plane", "image") == "object":
            freq = freq / m
        if freq <= 0:
            raise SaosError(ErrorCode.BAD_ARGS, "MTF needs a frequency")
        thr = float(_opt(mtf, "threshold", 0.3))
        c(Item("mtf", "target", "lower", thr, thr, "—", {"frequency": freq, "range": float(_opt(mtf, "range", 1.0))}))
    dist = _value(targets.get("distortion"))
    if dist is not None:
        thr = float(_need(dist, "threshold_pct", float))
        if thr <= 0:
            raise SaosError(ErrorCode.BAD_ARGS, "distortion threshold must be > 0")
        c(Item("distortion", "target", "upper", thr, thr, "%", {"range": float(_opt(dist, "range", 1.0))}))

    return Spec(
        raw=data,
        correction_class=cls,
        magnification=m,
        na=na,
        tube_lens_focal_length=f_tl,
        objective_field_number=ofn,
        receiver=receiver,
        pixel_size=float(pixel) if pixel else None,
        wavelengths_um=um,
        wavelength_weights=weights,
        cover_glass=cover,
        glass_catalogs=catalogs,
        glass_allowed_status=allowed,
        items=tuple(items),
        glass_allow_model=allow_model,
    )


def berek_check() -> dict[str, float]:
    """附录 A 表值核对用：返回 4×/0.10、10×/0.25、40×/0.65 的物方 Berek 景深（µm）。"""
    out = {}
    for m, na in ((4, 0.10), (10, 0.25), (40, 0.65)):
        out[f"{m}x/{na}"] = BEREK_OMEGA * 250000 / (na * 10 * m) + LAMBDA_E_UM / (2 * na**2)
    return out


__all__ = ["Item", "Spec", "load_spec", "parse_spec", "sample_points", "berek_check"]
