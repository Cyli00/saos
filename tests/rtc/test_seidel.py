"""逐面三阶像差（rtc.seidel）的核对，不启动 OpticStudio。

没有 OpticStudio 的逐面 Seidel 表可回放，所以从三处独立的量核对各面之和：
1. truth.json 里 OpticStudio 的 OPDC：轴上的四次项就是 W040，离轴的 ρ²、y² 项是场曲和像散；
2. 与 OpticStudio 逐条对过的实光线：孔径和视场缩到 1/10 后高阶项可以忽略，近轴像面上的横向像差
   由 S1、S2、S1 + 3·S3 + S4、S5 给出；
3. 已有真值的 PETZ：ΣS4 = H²·Σφ/(n·n′)。
"""

from __future__ import annotations

import copy

import numpy as np
import pytest

from case_variants import BY_NAME, CASES, truth
from rtc import firstorder, seidel as sd
from rtc.system import OpticalSystem
from rtc.trace import trace
from saos.backend.rtc import RtcBackend
from saos.protocol import ErrorCode, SaosError


def _sequential():
    return BY_NAME["sequential_opd"].lens()


def _shrunk(lens, factor: float):
    lens = copy.deepcopy(lens)
    lens.aperture_value *= factor
    lens.fields = [(x * factor, y * factor, w) for x, y, w in lens.fields]
    return lens


def _transverse(system, table: sd.Seidel) -> dict[str, tuple[float, float]]:
    """近轴像面上实光线的横向像差与三阶预计值，成对给出。最大视场在 +y 方向。"""
    k = len(system.items) - 2
    h, _, ua = table.marginal[k]
    hb, _, uba = table.chief[k]
    distance = -h / ua
    slope = system.items[k].n_after * ua
    ideal = hb + uba * distance

    def height(hy: float, py: float) -> float:
        block = trace(system, [0.0], [py], 0.0, hy)
        assert block.error[0] == 0 and block.vig[0] == 0
        return float(block.y[0, k] + block.m[0, k] / block.n[0, k] * (distance - block.z[0, k]))

    chief, upper, lower = height(1.0, 0.0) - ideal, height(1.0, 1.0) - ideal, height(1.0, -1.0) - ideal
    s1, s2, s3, s4, s5 = table.totals()[:5]
    return {"spherical": (height(0.0, 1.0), s1 / (2 * slope)), "distortion": (chief, s5 / (2 * slope)),
            "coma": (upper + lower - 2 * chief, 3 * s2 / slope), "astigmatism and Petzval": (upper - lower, (s1 + 3 * s3 + s4) / slope)}


def test_w040_is_the_quartic_term_of_opticstudios_on_axis_opd():
    system = OpticalSystem(_sequential())
    rows = np.asarray(truth("sequential_opd")["opd"][0]["rows"], dtype=float)
    rho2 = rows[:, 0] ** 2 + rows[:, 1] ** 2
    fit, *_ = np.linalg.lstsq(np.stack([rho2, rho2 ** 2, rho2 ** 3, rho2 ** 4], axis=1), rows[:, 2], rcond=None)
    assert sd.seidel(system).waves()["W040"].sum() == pytest.approx(fit[1], rel=1e-3)


def test_field_curvature_and_astigmatism_follow_opticstudios_off_axis_opd():
    system = OpticalSystem(_sequential())
    waves = {name: column.sum() for name, column in sd.seidel(system).waves().items()}
    field = truth("sequential_opd")["opd"][1]
    assert field["hx"] == 0.0 and field["hy"] == pytest.approx(0.6)
    rows = np.asarray(field["rows"], dtype=float)
    x, y = rows[:, 0], rows[:, 1]
    rho2 = x * x + y * y
    # W = W020ρ² + W222·H²·y² + W131·H·ρ²y + 倾斜 + 高阶项；W020 这里是 (W222/2 + W220P)·H²
    basis = np.stack([rho2, y * y, rho2 * y, y, rho2 ** 2, rho2 ** 2 * y, rho2 * y * y, rho2 ** 3], axis=1)
    fit, *_ = np.linalg.lstsq(basis, rows[:, 2], rcond=None)
    h2 = field["hy"] ** 2
    assert fit[1] == pytest.approx(waves["W222"] * h2, rel=0.02)
    assert fit[0] == pytest.approx((waves["W222"] / 2 + waves["W220P"]) * h2, rel=0.02)
    assert np.sign(fit[2]) == np.sign(waves["W131"])


@pytest.mark.parametrize("aspheric", [False, True])
def test_sums_match_real_ray_transverse_aberrations_at_small_aperture_and_field(aspheric):
    lens = _sequential()
    if aspheric:
        # 圆锥系数、偶次非球面的 α₁（改顶点曲率）和 α₂（四次项）一起改
        lens.surface(1).conic = -2.0
        lens.surface(3).type = "EVENASPH"
        lens.surface(3).params = {1: 2e-4, 2: 3e-6}
    system = OpticalSystem(_shrunk(lens, 0.1))
    table = sd.seidel(system)
    for name, (real, third_order) in _transverse(system, table).items():
        assert real == pytest.approx(third_order, rel=5e-3), name
    if aspheric:
        spherical = sd.seidel(OpticalSystem(_shrunk(_sequential(), 0.1))).totals()
        assert abs(table.totals()[0]) > 100 * abs(spherical[0])
        assert table.totals()[3] != pytest.approx(spherical[3], rel=1e-6)   # α₁ 改了光焦度


def test_petzval_column_gives_the_petz_radius():
    for name in ("sequential_opd", "iris"):
        system = OpticalSystem(BY_NAME[name].lens())
        table = sd.seidel(system)
        radius = -1.0 / (abs(system.items[-1].n_after) * table.totals()[3] / table.invariant ** 2)
        assert radius == pytest.approx(firstorder.petzval_radius(system), rel=1e-12)


def test_distortion_form_agrees_with_the_textbook_ratio_where_it_is_defined():
    # S5 = (Ā/A)(S3 + S4) 在 A = 0 的面上没有定义，实现用的是无奇点的写法；A ≠ 0 的面上两者必须相同
    system = OpticalSystem(BY_NAME["iris"].lens())
    table = sd.seidel(system)
    checked = 0
    for k, it in enumerate(system.items[:-1]):
        n = system.n_before(k)
        c = it.shape.vertex_curvature("y")
        a = n * (table.marginal[k][0] * c + table.marginal[k][1])
        abar = n * (table.chief[k][0] * c + table.chief[k][1])
        if abs(a) > 1e-6:
            assert table.rows[k, 4] == pytest.approx(abar / a * (table.rows[k, 2] + table.rows[k, 3]), rel=1e-9, abs=1e-15)
            checked += 1
    assert checked >= 4


def test_colour_columns_match_the_paraxial_focus_and_height_shifts():
    lens = _sequential()
    wavelengths = sorted(um for um, _ in lens.active_wavelengths)
    primary = OpticalSystem(lens)
    short, long = OpticalSystem(lens, wavelength_um=wavelengths[0]), OpticalSystem(lens, wavelength_um=wavelengths[-1])
    table = sd.seidel(primary, short, long)
    k = len(primary.items) - 2

    def image(system, plane=None):
        marginal, chief = sd.paraxial(system)
        focus = -marginal[k][0] / marginal[k][2]
        return focus, chief[k][0] + chief[k][2] * (focus if plane is None else plane)

    plane, _ = image(primary)
    slope = primary.items[k].n_after * table.marginal[k][2]
    focus_shift = image(short)[0] - image(long)[0]
    height_shift = image(short, plane)[1] - image(long, plane)[1]
    # 一阶色差公式略去 δn 的二次项，所以只到百分之几；CL > 0 表示短波的焦点更靠近镜头
    assert table.totals()[5] == pytest.approx(-slope * table.marginal[k][2] * focus_shift, rel=0.03)
    assert table.totals()[6] == pytest.approx(slope * height_shift, rel=0.03)
    assert focus_shift != 0.0 and sd.seidel(primary).totals()[5] == 0.0


def test_mirrors_and_ideal_lenses_are_refused():
    with pytest.raises(NotImplementedError, match="mirror"):
        sd.seidel(OpticalSystem(BY_NAME["reflective"].lens()))
    backend = RtcBackend(CASES / "reflective.zmx", standalone=True)
    for call in (lambda: backend.seidel(), lambda: backend.operand("SPHA", 0, 0, [])):
        with pytest.raises(SaosError) as err:
            call()
        assert err.value.code == ErrorCode.BACKEND_UNSUPPORTED


def test_backend_operands_and_table_are_the_same_numbers():
    backend = RtcBackend(CASES / "sequential.zmx", standalone=True)
    table = backend.seidel()
    assert table["columns"] == ["W040", "W131", "W222", "W220P", "W311", "W020", "W111"]
    assert [row["surface"] for row in table["surfaces"]] == [1, 2, 3]
    for operand, column in (("SPHA", "W040"), ("COMA", "W131"), ("ASTI", "W222"), ("FCUR", "W220P")):
        assert backend.operand(operand, 0, 0, []) == pytest.approx(table["totals"][column], rel=1e-12)
        assert [backend.operand(operand, row["surface"], 0, []) for row in table["surfaces"]] == pytest.approx(
            [row[column] for row in table["surfaces"]], rel=1e-12)
    # 逐面 DIST 是 W311；Int1=0 的 DIST 是最大视场的畸变百分比（同 DISG），不是 W311 之和
    assert [backend.operand("DIST", row["surface"], 0, []) for row in table["surfaces"]] == pytest.approx(
        [row["W311"] for row in table["surfaces"]], rel=1e-12)
    system = backend.optical_system(2)
    _, chief = sd.paraxial(system)
    k = len(system.items) - 1
    real = trace(system, [0.0], [0.0], 0.0, 1.0)
    assert real.error[0] == 0 and real.vig[0] == 0
    assert backend.operand("DIST", 0, 2, []) == pytest.approx(100.0 * (real.y[0, k] - chief[k][0]) / chief[k][0], rel=1e-12)
    assert backend.operand("DIST", 0, 2, []) != pytest.approx(table["totals"]["W311"], rel=1e-3)
    assert table["totals"]["W020"] != 0.0 and table["totals"]["W111"] != 0.0
    assert backend.operand("PETC", 0, 0, []) == pytest.approx(1.0 / backend.operand("PETZ", 0, 0, []), rel=1e-12)
    # 近轴边缘光线在最后一面给出后焦距，与 marginal-ray-height 求解定下的最后一段厚度一致
    last = table["surfaces"][-1]["surface"]
    back_focus = -backend.operand("PARY", last, 0, [0, 0, 0, 1]) / backend.operand("PATY", last, 0, [0, 0, 0, 1])
    assert back_focus == pytest.approx(backend.lens.thickness(last, 1), rel=1e-9)
    # SPHA 等四个已准入；整表带 W311，DIST 与 OpticStudio 的 Seidel 表不一致，仍未对账
    unverified = set(backend.provenance()["operands_unverified"])
    assert {"SEIDEL", "DIST", "PARY", "PETC"} <= unverified and not unverified & {"SPHA", "COMA", "ASTI", "FCUR"}
    with pytest.raises(SaosError) as err:
        backend.operand("SPHA", 9, 0, [])
    assert err.value.code == ErrorCode.BAD_ARGS


def test_paraxial_operands_use_the_field_angle_not_a_scaled_maximum_field():
    # 0.6 × 5° 必须按 tan 3° 取，不能取 0.6 × tan 5°：后者在像面差 8.6e-3 mm（2026-11-10 与 OpticStudio 的 PARY 实测）
    backend = RtcBackend(CASES / "sequential.zmx", standalone=True)
    system = backend.optical_system(2)
    marginal, chief = sd.paraxial(system, 0.6)
    _, maximum = sd.paraxial(system)
    for surface, k in ((1, 0), (3, 2), (4, 3)):
        assert backend.operand("PARY", surface, 2, [0.0, 0.6, 0.0, 1.0]) == pytest.approx(
            marginal[k][0] + chief[k][0], rel=1e-12)
        assert backend.operand("PATY", surface, 2, [0.0, 0.6, 0.0, 1.0]) == pytest.approx(
            marginal[k][2] + chief[k][2], rel=1e-12)
    assert abs(chief[3][0] - 0.6 * maximum[3][0]) > 1e-6


def test_starting_design_prediction_has_third_order_terms_on_rtc():
    from saos.starts.prediction import predict

    backend = RtcBackend(CASES / "sequential.zmx", standalone=True)
    prediction = predict(backend)
    assert prediction["errors"] == {} and prediction["native_analysis"] is None
    table = backend.seidel()["totals"]
    # 合计与 ctx.seidel 同一口径（各面之和，波数）；畸变百分比另列，不混进波数那一行
    assert [prediction["third_order"]["totals"][name] for name in ("SPHA", "COMA", "ASTI", "FCUR", "DIST")] == pytest.approx(
        [table[name] for name in ("W040", "W131", "W222", "W220P", "W311")], rel=1e-12)
    assert prediction["third_order"]["distortion_percent"] == pytest.approx(backend.operand("DIST", 0, 0, []), rel=1e-12)
    assert len(prediction["third_order"]["surfaces"]) == 3
    operands = truth("sequential_opd")["operands"]
    assert prediction["first_order"]["efl"] == pytest.approx(operands["EFFL"], rel=1e-9)
    assert prediction["petzval"]["radius"] == pytest.approx(operands["PETZ"], rel=1e-9)


def test_paraxial_chief_ray_uses_the_same_hy_as_the_real_trace_under_rectangular_normalisation():
    # 矩形归一化的 Hy 按 y 方向最大视场计；视场很小时近轴主光线与实际主光线在像面重合，口径不一致时差到 5/3 倍
    backend = RtcBackend(CASES / "sequential.zmx", standalone=True)
    backend.lens.fields = [(0.0, 0.0, 1.0), (0.04, 0.03, 1.0)]
    backend.lens.field_normalization = "rectangular"
    backend._systems.clear()
    image = len(backend.surfaces()) - 1
    paraxial = backend.operand("PARY", image, 0, [0.0, 1.0, 0.0, 0.0])
    real = backend.operand("REAY", image, 0, [0.0, 1.0, 0.0, 0.0])
    assert abs(real) > 1e-3 and paraxial == pytest.approx(real, rel=1e-5)
    assert abs(backend.operand("DIST", 0, 0, [])) < 1e-3
