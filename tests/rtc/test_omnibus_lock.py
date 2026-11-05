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

import numpy as np
import pytest

import omnibus
from rtc import adtrace, firstorder, geometry, zmx
from rtc import trace as tracing
from rtc.adtrace import Variable
from rtc.material.types import Mirror, Model
from rtc.operands import Operands
from rtc.system import OpticalSystem

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
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # piB 有不收敛的光线，按手册退回近轴发射，结果一样要锁住
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
                                                  ("BICONICZ", {1: 100.0}, 50.0), ("TOROIDAL", {1: 0.0}, 50.0)])
@pytest.mark.parametrize("axis", ["x", "y"])
def test_vertex_curvature_is_the_second_derivative_of_the_sag(kind, params, radius, axis):
    shape = geometry.make(zmx.Surface(index=1, type=kind, curvature=1.0 / radius, params=params))
    h = np.array([1e-3])
    zero = np.array([0.0])
    z = shape.sag(*((h, zero) if axis == "x" else (zero, h)))[0]
    assert shape.vertex_curvature(axis) == pytest.approx(float(np.ravel(z)[0]) * 2.0 / 1e-6, rel=1e-6, abs=1e-9)


@pytest.mark.parametrize("code, inside_corner", [(5, True), (7, False)])
def test_rectangular_and_elliptical_obscurations_differ_at_the_corner(code, inside_corner):
    """OpticStudio 真值的采样点碰巧分不开这两种遮挡（两个变体逐行相同），这里在角上直接区分。"""
    system = omnibus.systems(omnibus.BY_NAME["refractive_epd"])[2]
    item = system.items[3]
    item.surface.aperture_code, item.surface.ap_min, item.surface.ap_max = code, 0.8, 0.6
    item.surface.ap_dx, item.surface.ap_dy = 0.0, 0.0
    assert bool(tracing.aperture_clipped(item, np.array([0.75]), np.array([0.55]))[0]) is inside_corner
