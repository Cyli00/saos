"""全覆盖案例的快照：锁定 rtc 的光线和一阶量，再用差分和解析式独立核对雅可比与单步物理。

快照只证明结果没变，不证明正确；正确性由 test_case_truth.py 的 OpticStudio 真值回答。
确认某处结果应当改变时重录：RTC_LOCK_RECORD=1 uv run pytest tests/rtc/test_omnibus_lock.py -k snapshot
"""

from __future__ import annotations

import json
import os
import warnings
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import omnibus
from case_variants import BY_NAME
from rtc import adtrace, firstorder, geometry, kernel, zmx
from rtc import trace as tracing
from rtc.adtrace import Variable
from rtc.material.types import Mirror, Model
from rtc.operands import Operands
from rtc.system import OpticalSystem
from saos.backend.rtc import RtcBackend
from saos.ctx.experts.asphere import AsphereExpert

LOCK_FILE = Path(__file__).parent / "fixtures" / "golden" / "omnibus-lock.json"
RAY_KEYS = ("x", "y", "z", "l", "m", "n", "opl", "phase")
# 没有瞄准时只有舍入误差；瞄准的解取决于迭代终点，放宽两个量级
TOL = 1e-9
AIMED_TOL = 1e-7


def _rays(system, lens) -> np.ndarray:
    pupil = np.asarray(omnibus.PUPIL)
    rows = [(hx, hy, px, py) for hx, hy in omnibus.field_points(lens, system) for px, py in pupil]
    return np.asarray(rows)


def _trace(system, rays):
    return tracing.trace(system, rays[:, 2], rays[:, 3], rays[:, 0], rays[:, 1])


def _first_order(system) -> dict[str, float]:
    operands = Operands(system)
    quantities = {"EPDI": firstorder.entrance_pupil_diameter, "EFFL": firstorder.effl, "ENPP": firstorder.entrance_pupil_position,
                  "WFNO": firstorder.wfno, "TOTR": firstorder.totr, "PETZ": firstorder.petzval_radius,
                  "EXPP": lambda _: operands.expp(), "EXPD": lambda _: operands.expd()}
    out = {}
    for name, read in quantities.items():
        try:
            out[name] = float(read(system))
        except (ValueError, NotImplementedError) as exc:  # 边缘光线追不过、路径未对账时有定义地报错，错误本身也算行为
            out[name] = f"{type(exc).__name__}: {exc}"
    return out


def _snapshot(mode: omnibus.Mode) -> dict:
    systems = omnibus.systems(mode)
    out = {}
    for wave, system in systems.items():
        block = _trace(system, _rays(system, system.lens))
        out[str(wave)] = {**{key: getattr(block, key).tolist() for key in RAY_KEYS},
                          "error": block.error.tolist(), "vig": block.vig.tolist(), "first_order": _first_order(system)}
    return out


def _lock() -> dict:
    return json.loads(LOCK_FILE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("mode", omnibus.MODES, ids=lambda m: m.name)
def test_snapshot_is_unchanged(mode):
    got = _snapshot(mode)
    if os.environ.get("RTC_LOCK_RECORD") == "1":
        records = _lock() if LOCK_FILE.exists() else {}
        records[mode.name] = got
        ordered = {m.name: records[m.name] for m in omnibus.MODES if m.name in records}
        LOCK_FILE.write_text(json.dumps(ordered, separators=(",", ":")) + "\n", encoding="utf-8")
        return
    want = _lock()[mode.name]
    aimed = next(iter(omnibus.systems(mode).values())).lens.ray_aiming > 0
    tol = AIMED_TOL if aimed else TOL
    for wave, record in want.items():
        assert got[wave]["error"] == record["error"], (wave, "错误码")
        assert got[wave]["vig"] == record["vig"], (wave, "渐晕码")
        alive = np.asarray(record["error"]) == 0
        for key in RAY_KEYS:
            difference = np.abs(np.asarray(got[wave][key], float) - np.asarray(record[key], float))[alive]
            assert difference.max(initial=0.0) < tol, (wave, key, float(difference.max()))
        for name, value in record["first_order"].items():
            assert got[wave]["first_order"][name] == (value if isinstance(value, str) else pytest.approx(value, rel=1e-9, abs=1e-9)), (wave, name)


@pytest.mark.parametrize("mode", [m for m in omnibus.MODES if m.clips], ids=lambda m: m.name)
def test_the_rays_reach_every_aperture_they_are_meant_to_test(mode):
    """首挡面只报一个，后面的孔径可能被挡在前面的掩盖；断言每个孔径确实首挡过光线。"""
    system = next(iter(omnibus.systems(mode).values()))
    clips = Counter(_trace(system, _rays(system, system.lens)).vig.tolist())
    assert all(clips[surface] > 0 for surface in mode.clips), clips


# ---------------------------------------------------------------- 雅可比：与中心差分比，不依赖快照


def _variables(system) -> list[Variable]:
    out = []
    for k, it in enumerate(system.items[:-1]):
        if it.surface.curvature:
            out.append(Variable(k, "curvature"))
        if it.surface.conic:
            out.append(Variable(k, "conic"))
        out.append(Variable(k, "thickness"))
        out += [Variable(k, "param", p) for p, value in it.surface.params.items()
                if value and not (it.type in ("BINARY_2",) and p in (0, 13, 14))]
    if system.lens.aperture_type == "FLOA":
        out.append(Variable(system.stop, "semi_diameter"))
    return out


def _step(variable: Variable, value: float) -> float:
    return {"curvature": 1e-7, "thickness": 1e-5, "conic": 1e-5, "semi_diameter": 1e-5}.get(variable.field, 1e-6 * max(abs(value), 1e-3))


JACOBIAN_MODES = [m.name for m in omnibus.MODES]


@pytest.mark.parametrize("name", JACOBIAN_MODES)
@pytest.mark.parametrize("wave", ["primary", "other"])
def test_jacobian_matches_central_differences(name, wave):
    systems = omnibus.systems(omnibus.BY_NAME[name])
    lens = next(iter(systems.values())).lens
    system = systems[lens.primary_wave] if wave == "primary" else systems[min(systems)]
    variables = _variables(system)
    theta = np.array([v.value(system) for v in variables])
    rays = _rays(system, lens)
    image = system.items[-1].index
    got = adtrace.trace_ad(system, rays[:, 2], rays[:, 3], variables, theta, rays[:, 0], rays[:, 1]).at(image)
    base = _trace(system, rays)
    alive = base.error == 0
    if lens.ray_aiming and system.stop is not None:
        # 不收敛、退回近轴发射的光线在 θ 附近不连续，差分没有意义
        tx, ty = system.aim_target(*system.vignetted_pupil(rays[:, 2], rays[:, 3], rays[:, 0], rays[:, 1]))
        alive &= np.abs(base.x[:, system.stop] - tx) + np.abs(base.y[:, system.stop] - ty) < 1e-8
    for j, variable in enumerate(variables):
        h = _step(variable, theta[j])
        plus, minus = theta.copy(), theta.copy()
        plus[j] += h
        minus[j] -= h
        a = _trace(adtrace.system_with_variables(system, variables, plus), rays)
        b = _trace(adtrace.system_with_variables(system, variables, minus), rays)
        for key in ("x", "y", "l", "m", "n", "opl"):
            expected = (getattr(a, key)[:, -1] - getattr(b, key)[:, -1])[alive] / (2 * h)
            actual = got[key].d[j][alive]
            tolerance = 1e-5 * np.abs(expected).max(initial=0.0) + 1e-6
            assert np.abs(actual - expected).max(initial=0.0) < tolerance, (name, variable, key)


# ---------------------------------------------------------------- 单步物理


def _single_surface(kind="STANDARD", n2=None, mirror=False, **fields):
    """平行光射到一个面上，返回这个面的系统；相位和光焦度都只由这一面决定。"""
    surface = zmx.Surface(index=1, type=kind, thickness=10.0, **fields)
    surface.material = Mirror() if mirror else (Model(n2, 64.17) if n2 else None)
    lens = zmx.Lens(aperture_type="EPD", aperture_value=4.0, fields=[(0.0, 0.0, 1.0), (0.0, 5.0, 1.0)], num_fields=2, num_waves=1,
                    wavelengths=[(0.55, 1.0)], primary_wave=1)
    lens.vignetting = [(0.0,) * 5] * 2
    lens.surfaces = [zmx.Surface(index=0, thickness=float("inf")), surface, zmx.Surface(index=2)]
    surface.is_stop = True
    return OpticalSystem(lens)


def _unit_rays():
    v = np.linspace(-1.0, 1.0, 5)
    px, py = np.meshgrid(v, v)
    keep = px * px + py * py <= 1.0
    return px[keep], py[keep]


def test_refraction_is_diffraction_with_zero_phase():
    """同一个方程：相位系数全为零的二元面必须和普通非球面逐位一样。"""
    common = dict(curvature=0.02, conic=-0.7)
    plain = _single_surface("EVENASPH", n2=1.5168, params={1: 1e-5}, **common)
    binary = _single_surface("BINARY_2", n2=1.5168, params={0: 1.0, 1: 1e-5, 13: 2.0, 14: 5.0}, **common)
    px, py = _unit_rays()
    a, b = tracing.trace(plain, px, py, 0.0, 1.0), tracing.trace(binary, px, py, 0.0, 1.0)
    for key in RAY_KEYS:
        assert getattr(a, key) == pytest.approx(getattr(b, key), abs=1e-11), key


def test_a_phase_gradient_kicks_the_transverse_momentum():
    """平面上 n'·L' = n·L + (λ/2π)·∂φ/∂x，这是光栅方程本身；相位还要加到光程上。"""
    n2, rn, a1, a2 = 1.5, 5.0, -80.0, 6.0
    system = _single_surface("BINARY_2", n2=n2, params={0: 1.0, 13: 2.0, 14: rn, 15: a1, 16: a2})
    px, py = _unit_rays()
    block = tracing.trace(system, px, py, 0.0, 1.0)
    x, y = block.x[:, 0], block.y[:, 0]
    rho2 = (x * x + y * y) / rn**2
    g = 2.0 / rn**2 * (a1 + 2.0 * a2 * rho2)
    c = system.wavelength * 1e-3 / (2.0 * np.pi)
    incident = tracing.trace(_single_surface(), px, py, 0.0, 1.0)
    assert np.all(block.n[:, 0] > 0.0), "透射光要继续向前"
    n_after = system.items[0].n_after
    assert n_after * block.l[:, 0] == pytest.approx(incident.l[:, 0] + c * g * x, abs=1e-13)
    assert n_after * block.m[:, 0] == pytest.approx(incident.m[:, 0] + c * g * y, abs=1e-13)
    assert block.opl[:, 0] == pytest.approx(c * (a1 * rho2 + a2 * rho2**2), abs=1e-13)


def test_a_phase_gradient_on_a_curved_surface_conserves_momentum_along_the_tangents():
    """曲面上对两条切向量 τx = (1, 0, zx)、τy = (0, 1, zy) 都有 n'd'·τ = nd·τ + (λ/2π)·∂φ，法向分量才由 Snell 定。"""
    n2, rn, a1 = 1.5, 5.0, -80.0
    params = {0: 1.0, 13: 1.0, 14: rn, 15: a1}
    system = _single_surface("BINARY_2", n2=n2, curvature=0.2, params=params)
    px, py = np.array([0.7, -0.3, 0.0]), np.array([0.2, 0.6, -0.9])
    block = tracing.trace(system, px, py)
    x, y = block.x[:, 0], block.y[:, 0]
    _, zx, zy = system.items[0].shape.sag(x, y)
    c = system.wavelength * 1e-3 / (2.0 * np.pi)
    g = 2.0 * a1 / rn**2
    after = system.items[0].n_after * np.stack([block.l[:, 0], block.m[:, 0], block.n[:, 0]], axis=1)
    before = np.array([0.0, 0.0, 1.0])  # 轴上平行光
    for tangent, gradient in ((np.stack([np.ones_like(x), np.zeros_like(x), zx], axis=1), g * x),
                              (np.stack([np.zeros_like(x), np.ones_like(x), zy], axis=1), g * y)):
        assert np.sum((after - before) * tangent, axis=1) == pytest.approx(c * gradient, abs=1e-12)


def test_binary_phase_terms_beyond_the_term_count_have_no_derivative():
    """项数之外的系数不进相位，它的雅可比列要是零，和差分一致。"""
    system = _single_surface("BINARY_2", n2=1.5, curvature=0.02, params={0: 1.0, 13: 1.0, 14: 5.0, 15: -80.0, 16: 6.0})
    px, py = _unit_rays()
    variables = [Variable(0, "param", 15), Variable(0, "param", 16)]
    theta = np.array([-80.0, 6.0])
    got = adtrace.trace_ad(system, px, py, variables, theta).at(system.items[-1].index)
    assert np.any(got["x"].d[0] != 0.0)
    assert np.all(got["x"].d[1] == 0.0) and np.all(got["opl"].d[1] == 0.0)


@pytest.mark.parametrize("param", [0, 13, 14])
def test_binary_parameters_without_a_derivative_are_refused_as_variables(param):
    """级次、项数、归一化半径改变追迹结果，但切向没有实现：报错，不给静默的零列。"""
    system = _single_surface("BINARY_2", n2=1.5, curvature=0.02, params={0: 1.0, 13: 1.0, 14: 5.0, 15: -80.0})
    variables = [Variable(0, "param", param)]
    with pytest.raises(NotImplementedError, match="没有实现导数"):
        adtrace.trace_ad(system, [0.5], [0.0], variables, np.array([variables[0].value(system)]))


def test_reflection_is_the_other_root_of_the_same_equation():
    """反射 d' = d − 2(d·N)N，方向的切向分量不变。"""
    system = _single_surface(curvature=-0.01, conic=-1.0, mirror=True)
    px, py = _unit_rays()
    block = tracing.trace(system, px, py, 0.0, 1.0)
    incident = tracing.trace(_single_surface(), px, py, 0.0, 1.0)
    shape = system.items[0].shape
    _, zx, zy = shape.sag(block.x[:, 0], block.y[:, 0])
    w = np.sqrt(zx * zx + zy * zy + 1.0)
    N = np.stack([-zx / w, -zy / w, 1.0 / w], axis=1)
    d = np.stack([incident.l[:, 0], incident.m[:, 0], incident.n[:, 0]], axis=1)
    want = d - 2.0 * np.sum(d * N, axis=1, keepdims=True) * N
    got = -np.stack([block.l[:, 0], block.m[:, 0], block.n[:, 0]], axis=1)  # 报告值带 σ = −1
    assert got == pytest.approx(want, abs=1e-14)


@pytest.mark.parametrize("kind, params, radius", [("STANDARD", {}, 50.0), ("EVENASPH", {1: 1e-3}, 50.0), ("BICONIC", {1: 100.0}, 50.0),
                                                  ("BICONICX", {1: 100.0}, 50.0), ("BICONICX", {1: 0.0}, 50.0),
                                                  ("BICONICZ", {1: 100.0}, 50.0), ("BICONICZ", {1: 100.0, 14: 1e-3, 30: -2e-3}, 50.0),
                                                  ("TOROIDAL", {1: 0.0}, 50.0), ("TOROIDAL", {1: 0.0, 2: 1e-3}, 50.0)])
@pytest.mark.parametrize("axis", ["x", "y"])
def test_vertex_curvature_is_the_second_derivative_of_the_sag(kind, params, radius, axis):
    shape = geometry.make(zmx.Surface(index=1, type=kind, curvature=1.0 / radius, params=params))
    h = np.array([1e-3])
    zero = np.array([0.0])
    z = shape.sag(*((h, zero) if axis == "x" else (zero, h)))[0]
    assert shape.vertex_curvature(axis) == pytest.approx(float(np.ravel(z)[0]) * 2.0 / 1e-6, rel=1e-6, abs=1e-9)


def _paraxial_case(kind, params):
    """第 1 面是被测面，光阑在它后面，入瞳位置由这一面的近轴光焦度决定。"""
    surfaces = [omnibus._surface(1, kind, radius=30.0, thickness=4.0, material=omnibus.CROWN, params=params),
                omnibus._surface(2, thickness=4.0), omnibus._surface(3, thickness=30.0, stop=True, semi=2.0), omnibus._surface(4)]
    return OpticalSystem(omnibus._lens(surfaces, aperture=("EPD", 4.0), fields=[(0.0, 0.0, 1.0)]))


def test_diffractive_power_enters_the_paraxial_transfer():
    """相位的二次项有光焦度：OpticStudio 24.1 读数 ENPP = 7.527721402598309（2026-11-05），不计相位是 7.494。"""
    system = _paraxial_case("BINARY_2", {0: 1.0, 13: 1.0, 14: 5.0, 15: -80.0})
    assert firstorder.entrance_pupil_position(system) == pytest.approx(7.527721402598309, rel=1e-9)


def test_binary_term_count_cuts_the_rays_but_not_the_paraxial_power():
    """原始文件写 N=0 带 A_1：OpticStudio 的近轴量用 A_1，实光线不用（2026-11-05 实测）。"""
    params = {0: 1.0, 14: 5.0, 15: -80.0}
    hidden = _paraxial_case("BINARY_2", {**params, 13: 0.0})
    active = _paraxial_case("BINARY_2", {**params, 13: 1.0})
    bare = _paraxial_case("BINARY_2", {0: 1.0, 13: 0.0, 14: 5.0})
    assert firstorder.entrance_pupil_position(hidden) == pytest.approx(7.527721402598309, rel=1e-9)
    assert firstorder.entrance_pupil_position(bare) == pytest.approx(7.494, abs=1e-3)

    def slope(system):
        start = (np.array([0.0]), np.array([1.0]), np.array([0.0]), np.array([[0.0, 0.0, 1.0]]))
        return float(tracing._trace_block(system, [0.0], [0.0], start=start).m[0, 1])

    assert slope(hidden) == slope(bare)
    assert slope(hidden) != pytest.approx(slope(active), abs=1e-6)


@pytest.mark.parametrize("na, distance, expd", [(0.018, 135.0, 4.426293352262424), (0.1, 135.0, 24.71039698428348),
                                                (0.018, 60.0, 2.137495621770934)])
def test_finite_object_exit_pupil_diameter_uses_the_paraxial_stop_height(na, distance, expd):
    """瞄准关、有限物距：EXPD = 2·放大率·近轴光阑高度，读数来自 OpticStudio 24.1（2026-11-05）。"""
    surfaces = [omnibus._surface(1, radius=42.0, thickness=4.0, material=omnibus.CROWN),
                omnibus._surface(2, radius=-65.0, thickness=6.0),
                omnibus._surface(3, thickness=48.0, stop=True, semi=5.0), omnibus._surface(4)]
    lens = omnibus._lens(surfaces, aperture=("OBNA", na), object_distance=distance, fields=[(0.0, 0.0, 1.0), (0.0, 1.0, 1.0)])
    assert Operands(OpticalSystem(lens, allow_unverified=True)).expd() == pytest.approx(expd, abs=1e-6)


def _petzval_case(kind, params, fields=((0.0, 0.0, 1.0), (0.0, 1.0, 1.0))):
    surfaces = [omnibus._surface(1, kind, radius=50.0, conic=0.1, thickness=5.0, material=omnibus.CROWN, params=params),
                omnibus._surface(2, thickness=5.0), omnibus._surface(3, thickness=30.0, stop=True, semi=2.0),
                omnibus._surface(4, radius=-200.0, thickness=60.0), omnibus._surface(5)]
    return OpticalSystem(omnibus._lens(surfaces, aperture=("EPD", 2.0), fields=list(fields)))


@pytest.mark.parametrize("kind, params, want", [("STANDARD", {}, -146.74922383), ("EVENASPH", {1: 1e-3}, -133.40838530),
                                                ("TOROIDAL", {1: 0.0, 2: 1e-3}, -146.74922383), ("BICONICZ", {1: 80.0, 30: 1e-3}, -146.74922383),
                                                ("BICONICZ", {1: 80.0, 14: 1e-3}, -146.74922383)])
def test_petzval_counts_only_the_even_asphere_second_order_term(kind, params, want):
    """OpticStudio 24.1 读数（2026-11-05）：这些二次项都进近轴量，但 PETZ 只计偶次非球面的 α₁。"""
    assert firstorder.petzval_radius(_petzval_case(kind, params)) == pytest.approx(want, rel=1e-9)


def test_petzval_reads_zero_when_every_field_is_on_axis():
    assert firstorder.petzval_radius(_petzval_case("STANDARD", {}, fields=[(0.0, 0.0, 1.0)])) == 0.0


@pytest.mark.parametrize("name, want", [("piA-r012.zmx", -0.34637533), ("piB-r007.zmx", -0.24403057), ("codex-r017.zmx", 0.01096375)])
def test_petzval_uses_the_material_of_the_image_surface(name, want):
    """OpticStudio 24.1 读数（2026-11-05，加 1° 视场）：piA、piB 像面材料是空气，前一段介质 n = 1.32633；codex 两者相同。"""
    lens = zmx.parse(omnibus.LENSES / name)
    lens.fields, lens.num_fields = [(0.0, 0.0, 1.0), (0.0, 1.0, 1.0)], 2
    lens.vignetting = [(0.0,) * 5] * 2
    system = OpticalSystem(lens, wavelength_um=lens.active_wavelengths[lens.primary_wave - 1][0])
    assert firstorder.petzval_radius(system) == pytest.approx(want, rel=1e-6)  # 读数只留了 8 位小数


@pytest.mark.parametrize("params", [{0: 1.0, 13: 1.0, 15: -80.0}, {0: 1.0, 13: 1.0, 14: 0.0, 15: -80.0}])
def test_binary_normalization_radius_defaults_to_100(params):
    """OpticStudio 24.1（2026-11-05）：r_n 缺省或写 0 时读数和写 100 相同，写 1 时不同。"""
    explicit = _single_surface("BINARY_2", n2=1.5, curvature=0.02, params={**params, 14: 100.0})
    default = _single_surface("BINARY_2", n2=1.5, curvature=0.02, params=params)
    assert default.items[0].shape.row()[kernel.RN] == 100.0
    px, py = _unit_rays()
    assert tracing.trace(default, px, py, 0.0, 1.0).m == pytest.approx(tracing.trace(explicit, px, py, 0.0, 1.0).m, abs=1e-15)


def test_asphere_ranking_reports_the_rows_it_dropped(monkeypatch):
    """瞄准不收敛的光线导数是 NaN，不进残差；排名要说清楚丢了哪些。piB 的光阑 y 高度最大 0.02444，目标放到 0.03，y 向边缘的光线就够不到。"""
    monkeypatch.setattr(OpticalSystem, "_marginal_stop_hits", lambda self: (0.03, 0.03))
    with pytest.warns(RuntimeWarning, match="不收敛"):
        backend = RtcBackend(omnibus.LENSES / "piB-r007.zmx", standalone=True)
        ctx = SimpleNamespace(backend=backend, cache={}, surface=int)
        expert = AsphereExpert(ctx, list(range(1, len(backend.lens.surfaces) - 1)), terms=2, engine="rtc", grid=7, max_seconds=120,
                               single_sided=False, exclude_cemented=False)
        result = expert.rank()
    assert result["dropped_rows"] > 0
    assert result["residual_rows"] + result["dropped_rows"] == 2 * 29
    assert sum(entry["rows"] for entry in result["dropped"]) == result["dropped_rows"]


@pytest.mark.parametrize("code, inside_corner", [(5, True), (7, False)])
def test_rectangular_and_elliptical_obscurations_differ_at_the_corner(code, inside_corner):
    """OpticStudio 真值的采样点碰巧分不开这两种遮挡（两个变体逐行相同），这里在角上直接区分。"""
    system = omnibus.systems(omnibus.BY_NAME["refractive_epd"])[2]
    item = system.items[3]
    item.surface.aperture_code, item.surface.ap_min, item.surface.ap_max = code, 0.8, 0.6
    item.surface.ap_dx, item.surface.ap_dy = 0.0, 0.0
    assert bool(tracing.aperture_clipped(item, np.array([0.75]), np.array([0.55]))[0]) is inside_corner


def test_piB_rays_match_opticstudio_at_the_stop_without_a_warning():
    """OpticStudio 24.1（2026-11-05）：光阑落点是 (−0.03752733·px, −0.02444421·py)，两向不同、带符号；py = ±1 落在高度极值上。"""
    system = OpticalSystem(zmx.parse(omnibus.LENSES / "piB-r007.zmx"))
    px = np.array([0.5, 0.0, 1.0, -1.0, 0.0, 0.0, 0.6, -0.7])
    py = np.array([0.0, 0.5, 0.0, 0.0, 1.0, -1.0, 0.7, -0.6])
    want_x = np.array([-0.01876366, 0.0, -0.03752733, 0.03752733, 0.0, 0.0, -0.02251640, 0.02626913])
    want_y = np.array([0.0, -0.01222211, 0.0, 0.0, -0.02444421, 0.02444421, -0.01711095, 0.01466653])
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        block = tracing.trace(system, px, py)
    assert block.x[:, system.stop] == pytest.approx(want_x, abs=1e-8)
    assert block.y[:, system.stop] == pytest.approx(want_y, abs=1e-8)


def test_unconverged_direction_aiming_falls_back_to_a_forward_ray():
    """piB 的入瞳是虚的，发射方向朝 −z；瞄准不收敛时只能退回它的斜率，光线仍要朝 +z，方向和光程才对。光阑 y 高度最大 0.02444，目标放到 0.03 够不到。"""
    system = OpticalSystem(zmx.parse(omnibus.LENSES / "piB-r007.zmx"), allow_unverified=True, aim_scale=(0.03, 0.03))
    px, py = np.zeros(2), np.array([-1.0, 1.0])
    assert tracing.launch(system, px, py)[3][0, 2] < 0.0
    with pytest.warns(RuntimeWarning, match="不收敛"):
        block = tracing.trace(system, px, py)
    assert np.all(block.error == 0)
    assert np.all(block.n[:, 0] > 0.0)
    assert np.all(block.opl[:, -1] > 0.0)


def test_a_ray_lost_after_the_stop_keeps_the_aiming_correction_before_it():
    """光阑后才丢失的光线同样瞄准成功：读光阑面的导数要带隐函数项（落点锁在目标上，导数为零），不能当作没瞄准。"""
    lens = BY_NAME["iris_real"].lens()
    lens.surfaces[-1].curvature = 10.0  # 像面小得光线全打不中
    system = OpticalSystem(lens)
    px, py = np.array([0.0, 0.5]), np.array([0.9, 0.5])
    assert np.all(tracing.trace(system, px, py, 0.0, 1.0).error > system.items[system.stop].index)
    variables = [Variable(0, "curvature")]
    theta = np.array([variables[0].value(system)])
    after = system.items[system.stop + 1].index
    got = adtrace.trace_ad(system, px, py, variables, theta, 0.0, 1.0)
    assert got.at(system.items[system.stop].index)["y"].d[0] == pytest.approx(0.0, abs=1e-9)
    h = 1e-7
    plus = tracing.trace(adtrace.system_with_variables(system, variables, theta + h), px, py, 0.0, 1.0).at(after)["y"]
    minus = tracing.trace(adtrace.system_with_variables(system, variables, theta - h), px, py, 0.0, 1.0).at(after)["y"]
    assert got.at(after)["y"].d[0] == pytest.approx((plus - minus) / (2 * h), rel=1e-6)


def test_a_ray_that_missed_its_aim_has_no_derivative():
    """瞄准不收敛退回近轴发射的光线，值照常，导数标 NaN：发射方向的导数没有传播，缺项的有限值比没有更糟。"""
    mode = omnibus.BY_NAME["refractive_obna_finite"]
    lens = mode.build()
    wavelength = lens.active_wavelengths[lens.primary_wave - 1][0]
    # 目标定到够不着的地方，延拓一定不收敛
    system = OpticalSystem(lens, wavelength_um=wavelength, aim_scale=(100.0, 100.0), allow_unverified=True)
    variables = [Variable(0, "curvature")]
    with pytest.warns(RuntimeWarning, match="不收敛"):
        got = adtrace.trace_ad(system, [0.5, 0.0], [0.0, 0.5], variables, np.array([variables[0].value(system)])).at(system.items[-1].index)
    assert np.all(np.isfinite(got["x"].v))
    assert np.all(np.isnan(got["x"].d[0])) and np.all(np.isnan(got["opl"].d[0]))


@pytest.mark.parametrize("name", ["refractive_obna_finite_angle", "refractive_obna_finite_angle_real"])
def test_the_object_point_does_not_depend_on_the_wavelength(name):
    """角度视场的物点按主波长的入瞳定：OpticStudio 24.1 三个波长的物点逐位相同（2026-11-05），按各波长的入瞳算差 4e-4 mm。"""
    systems = omnibus.systems(omnibus.BY_NAME[name])
    assert len(systems) == 2
    (xa, ya), (xb, yb) = (system.object_point(0.6, 0.8) for system in systems.values())
    assert (xa, ya) == (xb, yb)
