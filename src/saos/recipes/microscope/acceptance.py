"""验收评估：按 docs/recipes/microscope/optical-spec-and-acceptance.md 第 3、4 节逐项判定，并计算规格差距。

流程：从设计文件取出物镜部分 → 按规格重建验收模型 → 只调工作距离对焦到轴上 e 线 c4 = 0
→ 检查光线 → 逐项测量 → 单项差距与规格差距。
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any

from saos import glasscat, prescription
from saos.backend import Backend
from saos.recipes.microscope import layout
from saos.protocol import ErrorCode, SaosError
from saos.recipes.microscope.spec import LAMBDA_E_UM, PFR_GRID, Spec, sample_points

ZERN_SAMP = 3  # 128×128
ZERN_MAX_TERM = 66  # 含 ρ¹⁰ 球差项；37 项时离轴点残差约 0.005λ，见第 3 步记录
ZERN_FIT_LIMIT = 0.005  # 波
FOCUS_MAX_ITER = 20
FOCUS_BACKOFF = 6  # 割线步落到光线失败位置时的二分回退次数
WAVE_F, WAVE_E, WAVE_C = 1, 2, 3
EDGE_RAYS = 36
ZONE_RAYS = 12
# 验收焦面上轴上波像差、平场比、视场波像差共用的 Zernike 项。同一波长、视场、工作距离只拟合一次：
# 拟合占一次验收评估耗时的约 9 成（实测每次 0.34–0.41 s），这些检查的视场点都在平场比网格内
FOCUS_PLANE_TERMS = (-1, -4, -8, 4, 5, 6)


class InvalidEvaluation(Exception):
    def __init__(self, reason: str, details: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


def axis_pupil() -> list[tuple[float, float]]:
    """轴上主光线和一圈边缘光线的归一化光瞳坐标。"""
    return [(0.0, 0.0)] + [(math.cos(2 * math.pi * k / EDGE_RAYS), math.sin(2 * math.pi * k / EDGE_RAYS)) for k in range(EDGE_RAYS)]


def focus_on_axis(
    backend: Backend,
    *,
    gap: int,
    wave: int,
    field: int,
    lam_um: float,
    na_image: float,
    magnification: float,
    tol_mm: float,
    log: list[dict[str, Any]] | None = None,
) -> float:
    """只改面 gap 的厚度（工作距离间隔），使轴上视场 field、波长 wave 的 Zernike 离焦 c4 = 0。

    c4 随物距近似线性变化，用割线法求根。首步按近轴关系估计：像方离焦 4√3·c4·λ/NA′²，
    除以 M² 得物方位移；符号不确定，由割线法自行修正。

    光线保护（第 5 步）：每个试探位置先追迹轴上主光线和 EDGE_RAYS 条边缘光线，有失败的位置不做
    Zernike 拟合（实测这类位置每次拟合约 31 s、残差上百波，c4 不可用）。
    - 起点失败：用近轴边缘光线高度 PARY = 0 求近轴焦点，从那里重新开始；仍失败则无法调焦。
    - 割线步落到失败位置：向上一个有效位置二分回退，最多 FOCUS_BACKOFF 次；都失败则无法调焦。
    所有位置都有效时，试探序列与不加保护时相同。log 记录每个试探位置，供诊断。
    """
    trail = log if log is not None else []
    pupil = axis_pupil()
    restarted = False

    def rays_ok(t: float, kind: str) -> bool:
        failures = backend.trace_check([(wave, 0.0, 0.0, px, py) for px, py in pupil])
        if failures:
            trail.append({"wave": wave, "kind": kind, "thickness": t, "ray_failures": len(failures)})
        return not failures

    def c4(t: float, kind: str) -> float:
        raw = backend.zernike(wave=wave, field=field, samp=ZERN_SAMP, max_term=ZERN_MAX_TERM, terms=(-1, 4))
        value = finite(raw[4], "c4 (focus)", wave=wave)
        trail.append({"wave": wave, "kind": kind, "thickness": t, "ray_failures": 0, "c4": value, "fit_error": raw.get(-1)})
        return value

    def move(t: float) -> None:
        backend.set_surface(gap, "thickness", t)

    t0 = finite(backend.surfaces()[gap]["thickness"], "working distance gap")
    if not rays_ok(t0, "start"):
        start = t0
        t0 = paraxial_focus(backend, gap=gap, wave=wave, t_start=t0, tol_mm=tol_mm, na_image=na_image, magnification=magnification)
        move(t0)
        restarted = True
        if not rays_ok(t0, "paraxial"):
            raise InvalidEvaluation("on-axis ray trace failed; cannot focus at the start or at the paraxial focus", {"wave": wave, "start_thickness": start, "paraxial_thickness": t0})
    c0 = c4(t0, "paraxial" if restarted else "start")
    step = 4 * math.sqrt(3) * c0 * lam_um / na_image**2 / magnification**2 / 1000
    t1 = t0 + (step if abs(step) > tol_mm else 10 * tol_mm)
    c1 = c0
    for _ in range(FOCUS_MAX_ITER):
        move(t1)
        if not rays_ok(t1, "secant"):
            # 二分回退：在上一个有效位置 t0 与失败位置之间找有效位置；只追迹，不拟合
            bad = t1
            for _ in range(FOCUS_BACKOFF):
                t1 = (t0 + bad) / 2
                move(t1)
                if rays_ok(t1, "backoff"):
                    break
                bad = t1
            else:
                move(t0)
                raise InvalidEvaluation(
                    "on-axis ray trace failed while focusing; no valid position after backing off",
                    {"wave": wave, "last_valid_thickness": t0, "last_valid_c4": c0, "nearest_failed_thickness": bad, "backoff": FOCUS_BACKOFF},
                )
        c1 = c4(t1, "secant")
        if abs(t1 - t0) < tol_mm and abs(c1) < 1e-4:
            return t1
        if c1 == c0:
            break
        t0, c0, t1 = t1, c1, t1 - c1 * (t1 - t0) / (c1 - c0)
    raise InvalidEvaluation("focusing did not converge", {"wave": wave, "last_thickness": t1, "last_c4": c1})


def paraxial_focus(
    backend: Backend, *, gap: int, wave: int, t_start: float, tol_mm: float, na_image: float, magnification: float
) -> float:
    """割线法求像面上近轴边缘光线高度 PARY = 0 的工作距离间隔。近轴量不受实际光线失败影响；
    实测（第 5 步两个起点）PARY 随间隔近似线性，2 次迭代收敛。

    收敛要求步长小于 tol_mm，且 PARY 不超过物方位移 tol_mm 在像面上造成的光线高度 NA′·M²·tol_mm；
    只看步长时，PARY 恒不为零的函数也可能因步长变小被误判为收敛。"""
    image = len(backend.surfaces()) - 1
    residual_limit = na_image * magnification**2 * tol_mm

    def pary(t: float) -> float:
        backend.set_surface(gap, "thickness", t)
        return finite(backend.operand("PARY", image, wave, [0.0, 0.0, 0.0, 1.0]), "PARY (paraxial focus)", wave=wave, thickness=t)

    t0, t1 = t_start, t_start + 0.01 * max(abs(t_start), 1.0)
    p0 = pary(t0)
    for _ in range(FOCUS_MAX_ITER):
        p1 = pary(t1)
        if abs(t1 - t0) < tol_mm and abs(p1) <= residual_limit:
            return t1
        if p1 == p0:
            break
        t0, p0, t1 = t1, p1, t1 - p1 * (t1 - t0) / (p1 - p0)
    raise InvalidEvaluation("paraxial focus did not converge", {"wave": wave, "last_thickness": t1, "last_pary": p1, "residual_limit": residual_limit})


def acceptance_config(spec: Spec) -> dict[str, Any]:
    """完整的验收配置：补全后的规格、全部达标线与尺度、派生量，以及评估器自身的常数。

    设计任务第 0 轮冻结它的哈希，之后各轮必须一致，规格差距才可比较（文档 4.3 节）。
    """
    return {
        "correction_class": spec.correction_class,
        "magnification": spec.magnification,
        "na": spec.na,
        "tube_lens_focal_length": spec.tube_lens_focal_length,
        "objective_field_number": spec.objective_field_number,
        "receiver": spec.receiver,
        "pixel_size": spec.pixel_size,
        "wavelengths_um": list(spec.wavelengths_um),
        "wavelength_weights": list(spec.wavelength_weights),
        "cover_glass": spec.cover_glass,
        "glass_catalogs": list(spec.glass_catalogs),
        "glass_allowed_status": list(spec.glass_allowed_status),
        "glass_allow_model": spec.glass_allow_model,
        "items": [
            {"id": i.id, "category": i.category, "kind": i.kind, "threshold": i.threshold, "scale": i.scale, "unit": i.unit, "params": i.params}
            for i in spec.items
        ],
        "derived": spec.derived(),
        "field_samples": spec.field_samples(),
        "evaluator": {
            "zern_samp": ZERN_SAMP,
            "zern_max_term": ZERN_MAX_TERM,
            "zern_fit_limit": ZERN_FIT_LIMIT,
            "focus_max_iter": FOCUS_MAX_ITER,
            "edge_rays": EDGE_RAYS,
            "focus_ray_guard": {"axis_rays": EDGE_RAYS + 1, "backoff": FOCUS_BACKOFF, "paraxial_restart": "PARY"},
            "zone_rays": ZONE_RAYS,
            "tube_lens_opd_mode": 1,
            "ray_aiming": "real",
        },
    }


def config_hash(config: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def finite(value: Any, what: str, **context: Any) -> float:
    """原始测量值必须是有限实数，否则本次评估无法完成。

    在测量值参与任何运算或 max/min 聚合之前调用：max(0, NaN) 会返回 0、NaN > 阈值为假，
    都会把无效数值悄悄变成"达标"。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InvalidEvaluation("invalid measurement (NaN, infinite or not a number)", {"what": what, "value": repr(value), **context})
    return float(value)


def residual_rms(rms_centroid: float, *removed: float) -> float:
    """从相对质心的均方根中扣除若干正交项后的均方根；拟合噪声造成的微小负值按 0 处理。"""
    total = finite(rms_centroid, "rms_centroid") ** 2
    for k, c in enumerate(removed):
        total -= finite(c, f"removed_term_{k}") ** 2
    return math.sqrt(max(0.0, total))


def field_wfe(rms_centroid: float, c4: float, c5: float, c6: float, *, remove_defocus_astig: bool) -> float:
    """视场波像差（docs/recipes/microscope/optical-spec-and-acceptance.md 3.3 节）。

    平场比同时启用时，离焦和像散已由平场比限定，扣除 c4–c6；否则在固定验收焦面上计入总波像差。
    """
    return residual_rms(rms_centroid, c4, c5, c6) if remove_defocus_astig else residual_rms(rms_centroid)


def reproduction_check(backend: Backend, design: list[dict[str, Any]], expect: dict[str, Any], primary_um: float) -> dict[str, Any]:
    """复现校验：在设计文件本身（专利的波长约定）上测焦距、数值孔径、工作距离和配参考筒镜的放大率。

    expect：{"efl": mm, "na": ..., "wd": mm, "magnification": ..., "rel_tol": 0.01}，缺的项不比较。
    """
    first, last = layout.objective_block(design)
    gap = layout.gap_surface(design)
    f_tl = design[last + 1]["focal_length"]
    efl = backend.operand("EFLY", first, last, [])
    na = backend.operand("OBSN", 0, 0, [])
    m = f_tl / efl
    primary = _primary_index(backend)
    wd_gap = focus_on_axis(backend, gap=gap, wave=primary, field=1, lam_um=primary_um, na_image=na / m, magnification=m, tol_mm=1e-5)
    sag = backend.operand("SAGY", first, 0, [])
    # 专利的工作距离按顶点距离给出；按 ISO 10934 计入前表面边缘凸出的机械工作距离另行报告
    # 放大率按 f_TL / f_obj 计。PMAG 在近轴像面上取值：物面偏离近轴前焦点 δ 时，中间像落在 f²/δ 处，
    # 筒镜后的像距随之缩短，PMAG 约小 f_TL·δ/f²（本例 δ = 1.5 µm 即 0.3%），只作诊断
    measured = {"efl": efl, "na": na, "wd": wd_gap, "magnification": m}
    pmag = abs(backend.operand("PMAG", 0, primary, []))
    tol = float(expect.get("rel_tol", 0.01))
    abs_tol = expect.get("tolerance_mm") or {}
    rows = []
    for key in ("efl", "na", "wd", "magnification"):
        if key in expect:
            want = float(expect[key])
            rel = abs(measured[key] / want - 1)
            # 容差取 1% 与原文印刷精度造成的舍入包络半宽中较大者
            allowed = max(tol * abs(want), float(abs_tol.get(key, 0.0)))
            rows.append(
                {"metric": key, "expected": want, "measured": measured[key], "rel_error": rel, "allowed_abs": allowed, "pass": abs(measured[key] - want) <= allowed}
            )
    return {
        "pass": all(r["pass"] for r in rows),
        "rel_tol": tol,
        "metrics": rows,
        "working_distance_mechanical_mm": wd_gap + min(0.0, sag),
        "front_sag_mm": sag,
        "pmag_at_best_focus": pmag,
    }


def _primary_index(backend: Backend) -> int:
    waves = backend.system_info()["wavelengths"]
    return next(i for i, w in enumerate(waves, 1) if w["primary"])


class Evaluator:
    def __init__(self, backend: Backend, spec: Spec, design_catalogs: list[str], telecentric_object: bool = False):
        self.backend = backend
        self.spec = spec
        self.design_catalogs = design_catalogs
        self.telecentric_object = telecentric_object
        self.fields: list[float] = spec.field_samples()
        self.field_index = {round(r, 6): i + 1 for i, r in enumerate(self.fields)}
        self.diagnostics: dict[str, Any] = {}
        self.focus_log: list[dict[str, Any]] = []
        self.m_act = spec.magnification
        self.t_gap = math.nan  # 当前工作距离间隔；只经 build_model、focus、set_thickness 改变
        self._zern: dict[tuple[int, float, float], dict[int, float]] = {}

    # ---- 验收模型 ----

    def build_model(self, design: list[dict[str, Any]]) -> None:
        spec = self.spec
        first, last = layout.objective_block(design)
        gap = layout.gap_surface(design)
        tube = design[last + 1]
        self.objective = [dict(r) for r in design[first : last + 1]]
        self.diagnostics["design_apertures"] = [
            {"surface": r["index"], "aperture": r["aperture"]} for r in design if r.get("aperture", "None") != "None"
        ]
        if not any(r["stop"] for r in self.objective) and not tube["stop"]:
            raise SaosError(ErrorCode.BAD_ARGS, "no stop in the objective part or on the reference tube lens")

        surfaces: list[dict[str, Any]] = []
        if spec.cover_glass:
            cover = spec.cover_glass
            obj = {
                "thickness": cover["thickness"],
                "material": {"model": {"nd": cover["nd"], "vd": cover["vd"], "dpgf": 0.0}},
                "comment": layout.TAG_OBJECT,
            }
            surfaces.append({"type": "standard", "radius": math.inf, "thickness": design[gap]["thickness"], "comment": layout.TAG_COVER_TOP})
        else:
            obj = {"thickness": design[gap]["thickness"], "material": "", "comment": layout.TAG_OBJECT}
        for r in self.objective:
            if r["type"] == "paraxial":
                surfaces.append({k: r[k] for k in ("type", "focal_length", "opd_mode", "thickness", "comment", "stop")})
            else:
                surfaces.append({**{k: r[k] for k in ("radius", "conic", "thickness", "material", "comment", "stop")}, "type": "standard"})
        f_tl = spec.tube_lens_focal_length
        surfaces.append({"type": "paraxial", "focal_length": f_tl, "opd_mode": 1, "thickness": f_tl, "comment": layout.TAG_TUBE, "stop": tube["stop"]})

        radius = spec.object_field_radius
        catalogs = list(dict.fromkeys([c.upper() for c in self.design_catalogs] + list(spec.glass_catalogs)))
        acc = prescription.normalize(
            {
                "title": "saos acceptance model",
                "aperture": {"type": "object_na", "value": spec.na},
                "fields": {"type": "object_height", "y": [r * radius for r in self.fields]},
                "wavelengths": {"um": list(spec.wavelengths_um), "weights": list(spec.wavelength_weights), "primary": WAVE_E},
                "catalogs": catalogs,
                "ray_aiming": "real",
                "telecentric_object": self.telecentric_object,
                "object": obj,
                "surfaces": surfaces,
            }
        )
        self.backend.build(acc)
        self.t_gap = design[gap]["thickness"]
        self.gap = 1 if spec.cover_glass else 0
        self.first = self.gap + 1
        self.last = self.first + len(self.objective) - 1
        self.tube = self.last + 1
        self.image = self.tube + 1

    # ---- 基本测量 ----

    def op(self, name: str, int1: int = 0, int2: int = 0, *data: float) -> float:
        return finite(self.backend.operand(name, int1, int2, list(data)), name, int1=int1, int2=int2, data=list(data))

    def checked_zern(self, wave: int, r: float, terms: tuple[int, ...]) -> dict[int, float]:
        key = (wave, round(r, 6), self.t_gap)
        if key not in self._zern:
            wanted = tuple(dict.fromkeys((*FOCUS_PLANE_TERMS, *terms)))
            raw = self.backend.zernike(wave=wave, field=self.field_index[key[1]], samp=ZERN_SAMP, max_term=ZERN_MAX_TERM, terms=wanted)
            z = {t: finite(c, f"ZERN {t}", wave=wave, field=r) for t, c in raw.items()}
            if z[-1] > ZERN_FIT_LIMIT:
                raise InvalidEvaluation("Zernike fit residual too large", {"wave": wave, "field": r, "fit_error": z[-1], "max_term": ZERN_MAX_TERM})
            self._zern[key] = z
        return {t: self._zern[key][t] for t in (-1, *terms)}

    def set_thickness(self, surface: int, value: float) -> None:
        self.backend.set_surface(surface, "thickness", value)
        if surface == self.gap:
            self.t_gap = value

    def na_image(self) -> float:
        return self.spec.na / self.m_act

    def defocus_um(self, c: float, wave: int) -> float:
        """Zernike Standard 离焦系数（波）换算成一次像面上的纵向离焦（µm）：4√3·c·λ/NA′²。"""
        lam = self.spec.wavelengths_um[wave - 1]
        return 4 * math.sqrt(3) * c * lam / self.na_image() ** 2

    def focus(self, wave: int) -> float:
        """只改工作距离间隔，使轴上该波长 c4 = 0；返回间隔厚度（mm）。"""
        # 调焦中途失败时位置未知，先作废当前位置，不复用拟合结果
        self.t_gap = math.nan
        self.t_gap = focus_on_axis(
            self.backend,
            gap=self.gap,
            wave=wave,
            field=self.field_index[0.0],
            lam_um=self.spec.wavelengths_um[wave - 1],
            na_image=self.na_image(),
            magnification=self.m_act,
            tol_mm=0.01 * self.spec.delta_ob_um / 1000,
            log=self.focus_log,
        )
        return self.t_gap

    # ---- 主流程 ----

    def run(self, design: list[dict[str, Any]]) -> dict[str, Any]:
        started = time.perf_counter()
        measured: dict[str, Any] = {}
        details: dict[str, Any] = {}
        try:
            self.build_model(design)
            self.m_act = self.spec.tube_lens_focal_length / self.op("EFLY", self.first, self.last)
            t_e = self.focus(WAVE_E)
            self.check_rays()
            measured, details = self.measure(t_e)
        except InvalidEvaluation as exc:
            return self.report(None, {}, {"reason": exc.reason, **exc.details}, started)
        return self.report(measured, details, None, started)

    def check_rays(self) -> None:
        pupil = axis_pupil()
        pupil += [(0.7 * math.cos(2 * math.pi * k / ZONE_RAYS), 0.7 * math.sin(2 * math.pi * k / ZONE_RAYS)) for k in range(ZONE_RAYS)]
        rays = [(w, 0.0, r, px, py) for r in self.fields for w in (WAVE_F, WAVE_E, WAVE_C) for px, py in pupil]
        failures = self.backend.trace_check(rays)
        if failures:
            raise InvalidEvaluation("check rays failed or were clipped", {"failures": failures[:20], "count": len(failures), "total": len(rays)})

    def measure(self, t_e: float) -> tuple[dict[str, Any], dict[str, Any]]:
        spec = self.spec
        v: dict[str, Any] = {}
        d: dict[str, Any] = {}
        rows = self.backend.snapshot()
        for r in rows[: self.image]:
            finite(r["thickness"], "thickness", surface=r["index"])
            finite(r["semi_diameter"], "semi-diameter", surface=r["index"])
        objective = rows[self.first : self.last + 1]
        glass_rows = [r for r in objective if r["material"]]
        lens_surfaces = [r["index"] for r in objective if r["material"] or rows[r["index"] - 1]["material"]]
        last_lens = max(lens_surfaces)

        v["magnification"] = abs(self.m_act / spec.magnification - 1)
        d["magnification"] = {"m_act": self.m_act, "f_obj_mm": spec.tube_lens_focal_length / self.m_act}
        d["na_object"] = self.op("OBSN")
        sag = self.op("SAGY", self.first)
        v["working_distance"] = t_e + min(0.0, sag)
        d["working_distance"] = {"gap_mm": t_e, "front_sag_mm": sag}
        v["optical_length"] = sum(r["thickness"] for r in rows[:last_lens])
        v["clear_diameter"] = 2 * max(r["semi_diameter"] for r in objective)

        v["glass_status"], d["glass_status"] = self.glass_violations(glass_rows)
        bad_types = [r["index"] for r in objective if r["type"] != "standard" or abs(r.get("conic", 0.0)) > 0]
        v["surface_types"] = float(len(bad_types))
        d["surface_types"] = bad_types
        v["elements"] = float(len(glass_rows))
        v["center_thickness_glass"] = min(r["thickness"] for r in glass_rows)
        edges = {r["index"]: self.op("ETVA", r["index"], 0, 0, 1) for r in glass_rows}
        v["edge_thickness_glass"] = min(edges.values())
        d["edge_thickness_glass"] = edges
        air = [r for r in objective if not r["material"] and r["index"] < last_lens and r["index"] in lens_surfaces]
        air_edges = {r["index"]: self.op("ETVA", r["index"], 0, 0, 1) for r in air}
        v["air_gap"] = min([r["thickness"] for r in air] + list(air_edges.values())) if air else math.inf
        d["air_gap"] = {"center": {r["index"]: r["thickness"] for r in air}, "edge": air_edges}

        axial = self.checked_zern(WAVE_E, 0.0, (-4, 4, -8))
        v["axial_rms_wfe"] = residual_rms(axial[-4], axial[4])
        d["axial_rms_wfe"] = {"rms_centroid": axial[-4], "c4": axial[4], "pv_centroid": axial[-8]}

        t_f = self.focus(WAVE_F)
        t_c = self.focus(WAVE_C)
        self.set_thickness(self.gap, t_e)
        v["axial_color"] = abs(t_c - t_f) * 1000
        d["axial_color"] = {"focus_F_um": (t_f - t_e) * 1000, "focus_C_um": (t_c - t_e) * 1000}

        oscs = {z: self.op("OSCD", 0, WAVE_E, z) for z in (0.7, 1.0)}
        v["osc"] = max(abs(x) for x in oscs.values())
        d["osc"] = oscs

        plan = self.plan_field()
        d["plan_field"] = plan
        if spec.item("plan_field"):
            v["plan_field"] = plan["excess"]

        item = spec.item("field_rms_wfe")
        if item:
            pts = {}
            for r in sample_points(item.params["range"]):
                z = self.checked_zern(WAVE_E, r, (-4, 4, 5, 6))
                pts[r] = field_wfe(z[-4], z[4], z[5], z[6], remove_defocus_astig=item.params["remove_defocus_astig"])
            v["field_rms_wfe"] = max(pts.values())
            d["field_rms_wfe"] = pts

        item = spec.item("lateral_color")
        if item:
            pts = {}
            for r in sample_points(item.params["range"]):
                if r > 0:
                    yf = self.op("REAY", self.image, WAVE_F, 0, r, 0, 0)
                    yc = self.op("REAY", self.image, WAVE_C, 0, r, 0, 0)
                    pts[r] = abs(yf - yc) * 1000
            v["lateral_color"] = max(pts.values())
            d["lateral_color"] = pts

        item = spec.item("mtf")
        if item:
            pts = {r: self.op("MTFN", 2, 0, self.field_index[round(r, 6)], item.params["frequency"]) for r in sample_points(item.params["range"])}
            v["mtf"] = min(pts.values())
            d["mtf"] = pts

        dist = {r: self.op("DISG", 1, WAVE_E, 0, r, 0, 0) for r in sample_points(spec.item("distortion").params["range"] if spec.item("distortion") else 1.0) if r > 0}
        d["distortion_pct"] = dist
        if spec.item("distortion"):
            v["distortion"] = max(abs(x) for x in dist.values())

        d["strehl_axis_e"] = self.op("STRH", 2, WAVE_E, 1)
        d["pmag_check"] = abs(-self.op("PMAG", 0, WAVE_E) / self.m_act - 1)
        return v, d

    def plan_field(self) -> dict[str, Any]:
        """平场：在验收焦面上由 c4、c5、c6 求 Δ 与像散差（µm），与 Berek 焦深 δ′ 比较。"""
        delta = self.spec.berek_image_um
        points = []
        r_max = 0.0
        broken = False
        excess = -math.inf
        for r in PFR_GRID:
            z = self.checked_zern(WAVE_E, r, (4, 5, 6))
            mean = abs(self.defocus_um(z[4], WAVE_E))
            lam = LAMBDA_E_UM
            astig = 4 * math.sqrt(6) * math.hypot(z[5], z[6]) * lam / self.na_image() ** 2
            ok = mean <= delta and astig <= delta
            if ok and not broken:
                r_max = r
            broken = broken or not ok
            if r <= 0.85 + 1e-9:
                excess = max(excess, mean / delta - 1, astig / delta - 1)
            points.append({"r": r, "mean_um": mean, "astig_um": astig})
        return {"delta_um": delta, "pfr": r_max, "pfn_mm": r_max * self.spec.objective_field_number, "excess": excess, "points": points}

    def glass_violations(self, glass_rows: list[dict[str, Any]]) -> tuple[float, list[dict[str, Any]]]:
        catalog = glasscat.load_catalogs(list(self.spec.glass_catalogs))
        by_name: dict[str, glasscat.Glass] = {}
        for g in catalog:
            by_name.setdefault(g.name.upper(), g)
        report = []
        bad = 0
        for r in glass_rows:
            m = r["material"]
            if isinstance(m, dict):
                ok, info = self.spec.glass_allow_model, {"model": m["model"]}
            elif m in by_name:
                g = by_name[m]
                ok, info = g.status in self.spec.glass_allowed_status, {"catalog": g.catalog, "status": g.status}
            else:
                ok, info = False, {"reason": "not in a glass catalog allowed by the spec"}
            bad += not ok
            report.append({"surface": r["index"], "material": m, "ok": ok, **info})
        return float(bad), report

    # ---- 结果 ----

    def report(self, measured: dict[str, Any] | None, details: dict[str, Any], invalid: dict[str, Any] | None, started: float) -> dict[str, Any]:
        if measured is not None and invalid is None:
            bad = {
                item.id: repr(measured.get(item.id))
                for item in self.spec.items
                if not item.valid_value(measured.get(item.id))
            }
            if bad:
                invalid = {"reason": "invalid measurement (NaN, infinite or missing)", "items": bad}
                measured = None
        items = []
        total = 0.0
        if measured is not None:
            for item in self.spec.items:
                value = measured.get(item.id)
                g = item.gap(value)
                total += g
                items.append(
                    {
                        "id": item.id,
                        "category": item.category,
                        "kind": item.kind,
                        "value": value,
                        "threshold": item.threshold,
                        "scale": item.scale,
                        "unit": item.unit,
                        "shortfall": g,
                        "pass": g == 0,
                        "details": details.get(item.id),
                    }
                )
        verdict = "invalid" if invalid else ("pass" if total == 0 else "fail")
        return {
            "verdict": verdict,
            "spec_shortfall": math.inf if invalid else total,
            "invalid": invalid,
            "items": items,
            "diagnostics": {**self.diagnostics, **{k: v for k, v in details.items() if k not in {i.id for i in self.spec.items}}},
            "derived": self.spec.derived(),
            "focus_trace": self.focus_log,
            "elapsed_s": round(time.perf_counter() - started, 2),
        }
