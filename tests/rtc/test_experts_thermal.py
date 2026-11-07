"""专家类在多结构镜头上的行为（rtc 实例），真值同 tests/rtc/test_thermal_truth.py。"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rtc import zmx  # noqa: E402
from saos import prescription  # noqa: E402
from saos.backend import rtc_lens  # noqa: E402
from saos.backend.rtc import RtcBackend  # noqa: E402
from saos.ctx.experts import AsphereExpert, GlassCandidate, GlassExpert  # noqa: E402
from saos.protocol import ErrorCode, SaosError  # noqa: E402
from saos.kit import KitMethods  # noqa: E402
from thermal_variants import TCE, TRUTH_FILE, base, truth  # noqa: E402

pytestmark = pytest.mark.skipif(not TRUTH_FILE.exists(), reason="还没有录热结构真值")
WIZARD = {"data": "Spot Radius", "reference": "Centroid", "rings": 2, "arms": 6}


def thermal(case="sequential", temperatures=(-40.0, 65.0)) -> RtcBackend:
    p = base(case)
    p.setdefault("environment", {})["adjust_index"] = True
    p["thermal"] = {"temperatures": list(temperatures), "housing_tce": TCE}
    backend = RtcBackend(None, standalone=True)
    backend.build(prescription.normalize(p))
    return backend


class Ctx(KitMethods):
    """工具层的脚本上下文够用：专家类只用 backend、cache 和 kit 方法。"""

    in_check = False

    def __init__(self, backend):
        self.backend, self.cache = backend, {}

    def mark_dirty(self):
        pass


def ctx_of(backend):
    return Ctx(backend)


def ready(backend) -> RtcBackend:
    backend.merit_wizard(WIZARD)
    backend.vary(1, "radius", True)
    backend.vary(3, "thickness", True)
    return backend


def zoom() -> RtcBackend:
    p = base("sequential")
    p["surfaces"][2].pop("solves", None)
    p["surfaces"][2]["thickness"] = 95.0
    p["configurations"] = [{"operand": "THIC", "params": [3], "values": [95.0, 97.5]}]
    backend = RtcBackend(None, standalone=True)
    backend.build(prescription.normalize(p))
    return backend


GLASSES = [GlassCandidate("SCHOTT", "N-BK7", 1.5168, 64.2, .5), GlassCandidate("SCHOTT", "N-K5", 1.5224, 59.5, .5),
           GlassCandidate("SCHOTT", "N-BAK4", 1.5688, 56.0, .5)]


def test_glass_search_compares_over_every_configuration_and_rows_follow_the_new_glass():
    backend = ready(thermal())
    result = GlassExpert(ctx_of(backend), [1], materials=GLASSES, neighbors=2, max_passes=1, max_seconds=60, cycles="1", single_optimization_pass=True).run()
    assert result["feasibility_completed"] and result["status"] != "infeasible"
    assert result["final_merit"] == pytest.approx(backend.merit_value(), rel=1e-9)
    assert result["final_merit"] <= result["initial_merit"]
    name = backend.get_param("1.material")
    glss = next(o for o in backend._configurations()["operands"] if o["type"] == "GLSS" and o["params"] == [1])
    assert glss["solves"] == ["Fixed", "ConfigPickup", "ConfigPickup"] and {v.upper() for v in glss["values"]} == {str(name).upper()}
    # 玻璃内厚度跟随的是新玻璃的膨胀系数 / the glass thickness follows the new glass's expansion
    alpha = backend._catalog().get(str(name), backend.lens.catalogs).tce * 1e-6
    thickness = next(o for o in backend._configurations()["operands"] if o["type"] == "THIC" and o["params"] == [1])
    assert thickness["values"][1] == pytest.approx(thickness["values"][0] * (1.0 + alpha * -60.0), rel=1e-12)


def test_glass_search_works_on_a_zoom_lens():
    backend = zoom()
    backend.merit_wizard(WIZARD)
    backend.vary(1, "radius", True)
    result = GlassExpert(ctx_of(backend), [1], materials=GLASSES, neighbors=2, max_passes=1, max_seconds=60, cycles="1", single_optimization_pass=True).run()
    assert result["feasibility_completed"]
    assert result["final_merit"] <= result["initial_merit"]


def test_experts_refuse_a_merit_function_that_misses_a_configuration():
    backend = thermal()
    backend.merit_wizard(WIZARD)
    backend.merit = [r for r in backend.merit[: next(k for k, r in enumerate(backend.merit) if r.type == "CONF" and r.int1 == 2)]]
    backend.vary(1, "radius", True)
    ctx = ctx_of(backend)
    for run in (lambda: GlassExpert(ctx, [1], materials=GLASSES).run(), lambda: AsphereExpert(ctx, [1]).rank(), lambda: AsphereExpert(ctx, [1]).run(nas=1)):
        with pytest.raises(SaosError) as err:
            run()
        assert err.value.code == ErrorCode.BAD_ARGS and "[2, 3]" in err.value.message


def test_experts_start_from_the_nominal_configuration():
    backend = ready(thermal())
    backend.select_configuration(2)
    for run in (lambda: GlassExpert(ctx_of(backend), [1], materials=GLASSES).run(), lambda: AsphereExpert(ctx_of(backend), [1]).rank()):
        with pytest.raises(SaosError) as err:
            run()
        assert err.value.code == ErrorCode.BAD_ARGS and "select that one" in err.value.message


def test_glass_search_refuses_a_surface_whose_glass_changes_between_configurations():
    p = base("sequential")
    p["configurations"] = [{"operand": "GLSS", "params": [1], "values": ["N-BK7", "N-K5"]}]
    backend = RtcBackend(None, standalone=True)
    backend.build(prescription.normalize(p))
    backend.merit_wizard(WIZARD)
    with pytest.raises(SaosError) as err:
        GlassExpert(ctx_of(backend), [1], materials=GLASSES).run()
    assert err.value.code == ErrorCode.BAD_ARGS and "changes glass" in err.value.message


def test_experts_on_opticstudio_still_refuse_several_configurations():
    from saos.ctx.experts._session import sequential

    backend = SimpleNamespace(system_info=lambda: {"backend": "zos", "mode": "sequential"}, configuration_count=lambda: 2)
    with pytest.raises(SaosError) as err:
        sequential(SimpleNamespace(backend=backend))
    assert err.value.code == ErrorCode.BACKEND_UNSUPPORTED and "rtc instance" in err.value.message


def test_converted_surface_has_the_rows_opticstudio_gives_an_asphere():
    """seq_3 转面 1 再写同样的系数，所有行、求解码、源行号和数值都等于 OpticStudio 对 seq_asphere 跑 Make Thermal 的结果。"""
    data, ref = truth("seq_3"), zmx.parse_text(truth("seq_asphere")["zmx"])
    backend = RtcBackend(None, standalone=True)
    backend.build(prescription.normalize({**base("sequential"), "environment": {"adjust_index": True},
                                          "thermal": {"temperatures": data["temperatures"], "housing_tce": data["tce"]}}))
    backend.change_surface_type(1, "EvenAspheric")
    backend.set_param("1.conic", -0.5)
    for key, value in (("par2", -3e-5), ("par3", 2e-7), ("par4", -1e-9)):
        backend.set_param(f"1.{key}", value)
    # 标称半口径两边各自算；比的是跟随规则，所以用真值的标称值
    for s in (1, 2, 3):
        backend.set_param(f"{s}.semi_diameter", ref.cell("SDIA", s, 1).value)
        backend.set_param(f"{s}.mech_semi_diameter", ref.cell("MCSD", s, 1).value)
    backend._configurations()
    mine = backend.lens
    assert [(r.op, r.surface, r.param, r.config, r.solve_code, r.source_config, int(r.solve[4])) for r in mine.mce] == \
           [(r.op, r.surface, r.param, r.config, r.solve_code, r.source_config, int(r.solve[4])) for r in ref.mce]
    for a, b in zip(mine.mce, ref.mce):
        if a.op != "GLSS":
            assert math.isclose(a.value, b.value, rel_tol=1e-9, abs_tol=2e-9), (a.op, a.surface, a.param, a.config, a.value, b.value)


def test_back_to_standard_restores_the_rows_and_their_source_numbers():
    backend = thermal()
    before = [(r.op, r.surface, r.config, int(r.solve[4])) for r in backend.lens.mce]
    backend.change_surface_type(1, "EvenAspheric")
    assert sum(r.op == "PRAM" for r in backend.lens.mce) == 8 * 3
    backend.change_surface_type(1, "Standard")
    assert [(r.op, r.surface, r.config, int(r.solve[4])) for r in backend.lens.mce] == before


def test_term_above_the_rows_is_refused_on_a_thermal_lens():
    backend = thermal()
    backend.change_surface_type(1, "EvenAspheric")
    with pytest.raises(SaosError) as err:
        backend.set_param("1.par9", 1e-12)
    assert err.value.code == ErrorCode.BACKEND_UNSUPPORTED
    with pytest.raises(SaosError):
        AsphereExpert(ctx_of(ready(thermal())), [1], terms=8).rank()


@pytest.mark.parametrize("engine", ["rtc", "opticstudio"])
def test_rank_leaves_the_lens_unchanged(engine):
    backend = ready(thermal())
    before = backend._configurations()
    merit = backend.merit_value()
    result = AsphereExpert(ctx_of(backend), [1, 2, 3], terms=2, engine=engine, max_seconds=60).rank()
    assert result["order"] or result["selected"]
    assert backend._configurations() == before
    assert backend.merit_value() == pytest.approx(merit, rel=1e-12)
    assert backend.current_configuration() == 1


def test_rtc_ranking_stacks_every_configuration():
    """残差行数是每个结构的和：三结构比单结构多两倍。"""
    single = RtcBackend(None, standalone=True)
    single.build(prescription.normalize(base("sequential")))
    one = AsphereExpert(ctx_of(single), [1], terms=2, engine="rtc", max_seconds=60).rank()
    backend = thermal()
    three = AsphereExpert(ctx_of(backend), [1], terms=2, engine="rtc", max_seconds=60).rank()
    assert one["dropped_rows"] == three["dropped_rows"] == 0
    assert three["residual_rows"] == 3 * one["residual_rows"]


def test_asphere_run_makes_terms_that_follow_the_temperature(tmp_path):
    backend = ready(thermal())
    before = backend.merit_value()
    result = AsphereExpert(ctx_of(backend), [1], terms=2, max_seconds=120, cycles="10", engine="rtc").run(nas=1)
    assert result["installed"], result["status"]
    assert result["installed"][0]["after"] < before
    assert backend.merit_value() < before
    par2 = next(o for o in backend._configurations()["operands"] if o["type"] == "PRAM" and o["params"] == [1, 2])
    assert par2["solves"][0] == "Variable" and par2["solves"][1:] == ["ThermalPickup", "ThermalPickup"]
    a = 7.1e-6 * -60.0
    assert par2["values"][0] != 0.0
    assert par2["values"][1] == pytest.approx(par2["values"][0] / (1.0 + a) ** 3, rel=1e-12)
    # 写盘再读回，变量和行都在 / saved and read back, the variables and rows stay
    path = tmp_path / "asphere.zmx"
    backend.save_as(path)
    again = RtcBackend(None, standalone=True)
    again.load(path)
    assert again.variables() == backend.variables()
    assert again.merit_value() == pytest.approx(backend.merit_value(), rel=1e-12)
    assert rtc_lens.SURFACE_PASSTHROUGH[0] == "TCED"


def test_asphere_rank_on_a_zoom_lens_has_a_focus_column_per_configuration():
    backend = zoom()
    result = AsphereExpert(ctx_of(backend), [1], terms=2, engine="rtc", max_seconds=60).rank()
    assert result["dropped_rows"] == 0 and result["selected"]


def test_focus_columns_count_what_can_really_be_adjusted():
    from saos.ctx.experts.asphere import _focus_column

    follow = thermal().lens
    assert {_focus_column(follow, 3, c) for c in (1, 2, 3)} == {0}
    independent = zoom().lens
    assert [_focus_column(independent, 3, c) for c in (1, 2)] == [0, 1]
    # 变焦行在别的面：最后厚度各结构共用 / the zoom row elsewhere: the last thickness is shared
    assert {_focus_column(independent, 2, c) for c in (1, 2)} == {0}


def test_model_surfaces_are_found_on_the_rtc_instance():
    p = base("sequential")
    p["surfaces"][0]["material"] = {"model": {"nd": 1.6, "vd": 45.0}}
    backend = RtcBackend(None, standalone=True)
    backend.build(prescription.normalize(p))
    assert GlassExpert(ctx_of(backend), "model", materials=GLASSES)._surfaces() == [1]
