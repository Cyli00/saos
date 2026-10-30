"""rtc 实例上的设计轮次（ADR-0017）的离线回归，不开 OpticStudio：评价函数、带精确约束的局部优化、搜索、.zmx 往返、
结构指纹，以及复测记录在 ledger 里的折叠。

镜头取 tests/fixtures/cases/sequential.zmx（OpticStudio 24.1.0 存盘的胶合双片）与 tests/fixtures/rtc/doublet.*（OpticStudio 存出的
双片和它的 describe_model）。玻璃取本机 Documents/Zemax/Glasscat，找不到 SCHOTT.AGF 时跳过用目录玻璃的测试。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from saos import ledger, model_gate
from saos.backend import rtc_merit
from saos.backend.rtc import RtcBackend
from saos.protocol import ErrorCode, SaosError

CASES = Path(__file__).parent / "fixtures" / "cases"
RTC_FIXTURES = Path(__file__).parent / "fixtures" / "rtc"
WIZARD = {"rings": 3, "arms": 6, "glass": [1.0, 10.0, 1.0], "air": [0.5, 50.0, 0.5]}


def _glasscat_ready() -> bool:
    from rtc.materials import catalog_dir

    return any(p.stem.upper() == "SCHOTT" for p in catalog_dir().glob("*.[Aa][Gg][Ff]"))


needs_glass = pytest.mark.skipif(not _glasscat_ready(), reason="需要本机 Documents/Zemax/Glasscat/SCHOTT.AGF")


def _loaded(path: Path = CASES / "sequential.zmx") -> RtcBackend:
    backend = RtcBackend(None, standalone=True)
    backend.load(path)
    return backend


def _designed() -> RtcBackend:
    backend = _loaded()
    for surface in (1, 2, 3):
        backend.vary(surface, "radius", True)
    backend.merit_wizard(WIZARD)
    return backend


@needs_glass
def test_boundary_rows_read_their_target_when_met_and_count_nothing():
    backend = _loaded()
    # 第 1 面厚 4 mm：CTGT 3 满足（值为目标、不计入），CTGT 5 越界（值为实际厚度）
    backend.merit_add([{"type": "CTGT", "int1": 1, "target": 3.0, "weight": 1.0},
                       {"type": "CTGT", "int1": 1, "target": 5.0, "weight": 1.0}])
    rows = {r["row"]: r for r in backend.merit_rows()}
    met, broken = rows[2], rows[3]
    assert met["value"] == 3.0 and met["contribution"] == 0.0
    assert broken["value"] == pytest.approx(4.0) and broken["contribution"] == pytest.approx(100.0)
    # MF² = ΣW(V−T)²/ΣW = (0 + 1) / 2
    assert backend.merit_value() == pytest.approx(math.sqrt(0.5))


@needs_glass
def test_local_optimisation_lowers_the_merit_and_meets_every_boundary_exactly():
    backend = _designed()
    # 第 1 面厚度也是变量，且必须 ≥ 5 mm（起点 4 mm）：精确约束，不能以一点违背换像差
    backend.vary(1, "thickness", True)
    backend.merit_add([{"type": "CTGT", "int1": 1, "target": 5.0, "weight": 1.0}])
    result = backend.optimize("auto", max_seconds=60)
    assert result["final_merit"] < 0.1 * result["initial_merit"]
    # 约束靠有效集精确满足，剩下的只是非线性边界（边缘厚度）在 1e-9 相对量级的数值残差
    assert result["constraints"] > 0 and result["violation"]["after"] <= 1e-6
    assert backend.get_param("1.thickness") >= 5.0 * (1 - 1e-9)
    assert result["algorithm"] == "dls" and result["backend"] == "rtc"


@needs_glass
def test_merit_rows_rtc_cannot_compute_are_refused_as_a_batch():
    backend = _designed()
    before = len(backend.merit)
    with pytest.raises(SaosError) as err:
        backend.merit_add([{"type": "CTGT", "int1": 1, "target": 3.0}, {"type": "ETGT", "int1": 1, "target": 1.0}])
    assert err.value.code == ErrorCode.BACKEND_UNSUPPORTED and "ETGT" in err.value.message
    assert len(backend.merit) == before


@needs_glass
def test_wizard_rows_sample_the_pupil_by_gaussian_quadrature():
    backend = _loaded()
    rows = rtc_merit.wizard_rows(backend.lens, {"rings": 4, "arms": 8})
    first = [r for r in rows if r.type == "OPDX"][:16]
    radii = sorted({round(math.hypot(r.data[2], r.data[3]), 12) for r in first})
    import numpy as np

    nodes, _ = np.polynomial.legendre.leggauss(4)
    assert radii == pytest.approx(sorted(np.sqrt((1 + nodes) / 2)))
    angles = sorted({round(math.degrees(math.atan2(r.data[3], r.data[2])), 9) for r in first})
    assert angles == pytest.approx([-67.5, -22.5, 22.5, 67.5])


@needs_glass
def test_saved_lens_keeps_merit_rows_variables_and_carried_lines(tmp_path):
    from rtc.textio import decode_text

    backend = _designed()
    backend.optimize("5", max_seconds=30)
    target = tmp_path / "work.zmx"
    backend.save_as(target)
    text = decode_text(target.read_bytes())
    source = decode_text((CASES / "sequential.zmx").read_bytes())
    # rtc 不建模的文件头行原样带过去 / header lines rtc does not model are carried over
    for line in source.splitlines():
        if line.startswith(("POLS", "GSTD", "COFN")):
            assert line.strip() in text
    again = _loaded(target)
    assert again.variables() == backend.variables()
    assert [(r.type, r.params, r.target, r.weight) for r in again.merit] == [(r.type, r.params, r.target, r.weight) for r in backend.merit]
    assert again.merit_value() == pytest.approx(backend.merit_value(), rel=1e-12)


@needs_glass
def test_unknown_surface_lines_are_refused(tmp_path):
    from rtc.textio import decode_text

    import re

    text = re.sub(r"SURF 2(\r?\n)", r"SURF 2\1  PZUP 1 1 0\1", decode_text((CASES / "sequential.zmx").read_bytes()), count=1)
    assert "PZUP" in text
    path = tmp_path / "pickup.zmx"
    path.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
    with pytest.raises(SaosError) as err:
        _loaded(path)
    assert err.value.code == ErrorCode.BACKEND_UNSUPPORTED and "PZUP" in err.value.message


@needs_glass
def test_hammer_keeps_the_start_or_finds_a_feasible_better_lens(tmp_path):
    backend = _designed()
    backend.save_as(tmp_path / "work.zmx")
    backend.optimize("auto", max_seconds=30)
    start = backend.merit_value()
    backend.vary(1, "material", True, "substitute", "")
    result = backend.optimize_search("hammer", max_seconds=3)
    assert result["final_merit"] <= start * (1 + 1e-12)
    assert result["stopped_by"] == "timeout" and result["systems"] >= 1
    # 找到可行解时最好的系统写在镜头文件旁，等后台进程归档 / the best feasible system is written next to the lens
    assert (tmp_path / "work_0000_HAMMER.zmx").is_file() == result["solver_succeeded"]


@needs_glass
def test_description_fingerprint_matches_opticstudio_apart_from_unread_settings():
    recorded = json.loads((RTC_FIXTURES / "doublet.json").read_text(encoding="utf-8"))
    desc = _loaded(RTC_FIXTURES / "doublet.zmx").describe_model(None)
    approved = model_gate.fingerprint(recorded["model"])
    assert model_gate.fingerprint_changes(approved, model_gate.fingerprint(desc), desc) == []
    assert sorted(desc["unread"]) == ["apodization", "coating_file", "polarization"]
    # OpticStudio 自己的描述没有 unread：复测时这三项照常比较
    assert model_gate.fingerprint_changes(approved, model_gate.fingerprint(desc), recorded["model"]) == \
        ["apodization", "coating_file", "polarization"]


@needs_glass
def test_fft_mtf_is_one_at_zero_frequency_and_falls_to_zero_at_cutoff():
    backend = _designed()
    backend.optimize("auto", max_seconds=30)
    out = backend.analysis("FftMtf", {"ShowDiffractionLimit": True, "SampleSize": "S_32x32"}, Path("unused.txt"))
    assert out["status"] == "ok" and out["series"][0]["description"] == "Diff. Limit"
    assert len(out["series"]) == 1 + len(backend.lens.fields)
    for series in out["series"]:
        for curve in series["y"].values():
            assert curve[0] == pytest.approx(1.0, abs=1e-9)
            # 截止频率处只剩一个像素的重叠 / at the cutoff only one pixel of the pupils overlaps
            assert curve[-1] == pytest.approx(0.0, abs=1e-2)


# ---- 复测记录 / confirmations ----

def _round(n: int, **extra) -> dict:
    return {"round": n, "tier": "local", "tiers": ["local"], "shortfall": 1.0, "achieved": False, "values": {"a": 1.0},
            "hard_failing": [], "measurement_errors": [], "model_glass": [], "evaluation": f"evaluations/{n:03d}/result.json",
            "demand_sha": "d", "method_sha": "m", "backend": "rtc", **extra}


def test_a_confirmation_replaces_the_numbers_of_the_round_it_confirms():
    rows = [_round(0, tier="baseline", tiers=["baseline"]), _round(1, achieved=True, shortfall=0.0),
            {**_round(2, achieved=False, shortfall=0.4, values={"a": 1.2}), "tier": ledger.CONFIRM, "tiers": [ledger.CONFIRM],
             "confirm_of": 1, "agrees": False, "backend": "zos"}]
    rounds = ledger.effective(rows)
    assert [r["round"] for r in rounds] == [0, 1]
    confirmed = rounds[1]
    assert confirmed["confirmed_by"] == 2 and confirmed["agrees"] is False and confirmed["measured_on"] == "zos"
    assert confirmed["shortfall"] == 0.4 and confirmed["achieved"] is False and confirmed["values"] == {"a": 1.2}
    assert confirmed["rtc"]["shortfall"] == 0.0 and confirmed["rtc"]["achieved"] is True
    assert ledger.confirmed(confirmed) and not ledger.confirmed(rounds[0])


def test_a_confirmation_under_other_checks_is_ignored():
    rows = [_round(0), {**_round(1, shortfall=9.0), "tier": ledger.CONFIRM, "confirm_of": 0, "method_sha": "other"}]
    rounds = ledger.effective(rows)
    assert rounds[0]["shortfall"] == 1.0 and "confirmed_by" not in rounds[0]


def test_the_delivered_round_is_the_achieved_one_with_the_smallest_shortfall():
    window = [_round(0, shortfall=3.0), _round(1, achieved=True, shortfall=0.5), _round(2, achieved=True, shortfall=0.5),
              _round(3, shortfall=0.1, model_glass=[2])]
    assert ledger.deliver_round(window)["round"] == 2
    assert ledger.deliver_round([_round(0, shortfall=math.inf)])["round"] == 0


@pytest.mark.parametrize("op, expected", [("SUMM", 3.0), ("DIFF", 3.0), ("PROD", 0.0)])
def test_arithmetic_with_a_zero_second_operand(op, expected):
    row = rtc_merit.Row(op, [1, 2, 0, 0, 0, 0, 0, 0])
    assert rtc_merit.Evaluator._arithmetic(row, [3.0, 0.0]) == expected
    row.type = "DIVI"
    with pytest.raises(ZeroDivisionError):
        rtc_merit.Evaluator._arithmetic(row, [3.0, 0.0])


@needs_glass
@pytest.mark.parametrize("column", [0, 6, 7])
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_nonfinite_merit_input_is_rejected_without_replacing_the_lens(tmp_path, column, value):
    from rtc.textio import decode_text

    backend = _loaded()
    original = backend.lens
    numbers = [0.0] * 10
    numbers[column] = value
    text = decode_text((CASES / "sequential.zmx").read_bytes())
    path = tmp_path / "invalid.zmx"
    path.write_bytes(b"\xff\xfe" + (text + "\nCONS " + " ".join(map(str, numbers)) + "\n").encode("utf-16-le"))
    with pytest.raises(SaosError, match=f"column {column + 1}") as error:
        backend.load(path)
    assert error.value.code == ErrorCode.BAD_ARGS
    assert backend.lens is original


@needs_glass
def test_continuously_varied_model_glass_does_not_grow_the_shared_cache():
    from rtc.material import Model

    backend = _loaded()
    size = len(backend._media)
    for i in range(100):
        backend.set_param("1.material", {"nd": 1.5 + i * 1e-5, "vd": 60.0})
        backend._system(2)
    assert len(backend._media) == size
    assert all(not isinstance(key[0], Model) for key in backend._media)
    assert backend.operand("INDX", 1, 2, []) > 1.5


@needs_glass
def test_batched_ray_operands_match_individual_operands(monkeypatch):
    from rtc import trace as trace_module
    from saos.ctx.rows import compile_rows

    backend = _loaded()
    specs = [{"type": op, "int1": surface, "int2": 2, "data": [0, 0.6, 0, py], "weight": 1}
             for surface in (1, 3, 4) for py in (-0.5, 0.0, 0.5) for op in ("REAX", "REAY", "REAC", "RAID")]
    expected = [backend.operand(s["type"], s["int1"], s["int2"], s["data"]) for s in specs]
    rows = rtc_merit.from_specs(compile_rows(specs, 1))
    evaluator = rtc_merit.Evaluator(backend, rows)
    calls = []
    trace = trace_module.trace

    def counted(*args, **kwargs):
        calls.append(1)
        return trace(*args, **kwargs)

    monkeypatch.setattr(trace_module, "trace", counted)
    for _ in range(2):
        result = evaluator.evaluate()
        assert not result.failed
        assert result.values == pytest.approx(expected, abs=1e-12)
    assert len(calls) == 2


@needs_glass
def test_wavefront_evaluation_uses_one_trace_per_field_and_wavelength(monkeypatch):
    from rtc import trace as trace_module
    from rtc.psf import opd_rays

    backend = _loaded()
    rows = rtc_merit.wizard_rows(backend.lens, {"rings": 3, "arms": 6, "reference": "Chief Ray"})
    evaluator = rtc_merit.Evaluator(backend, rows)
    expected = []
    for row in rows:
        if row.type == "OPDC":
            opd, ok = opd_rays(backend._system(row.int2), [row.data[2]], [row.data[3]], *row.data[:2])
            assert ok[0]
            expected.append(opd[0] / (backend.lens.active_wavelengths[row.int2 - 1][0] * 1e-3))
    trace = trace_module.trace
    calls = []

    def counted(*args, **kwargs):
        calls.append(1)
        return trace(*args, **kwargs)

    monkeypatch.setattr(trace_module, "trace", counted)
    result = evaluator.evaluate()
    assert [v for row, v in zip(rows, result.values) if row.type == "OPDC"] == pytest.approx(expected, abs=1e-9)
    assert len(calls) == len(backend.lens.fields) * len(backend.lens.active_wavelengths)


@needs_glass
@pytest.mark.parametrize("stop", ["cancel", "timeout", "error"])
def test_optimisation_restores_the_accepted_point_when_interrupted_during_differences(monkeypatch, stop):
    import time
    import numpy as np
    from saos.backend import rtc_optim

    backend = _designed()
    problem = backend._problem()
    initial = problem.values()
    evaluate = problem.evaluator.evaluate
    measurements = []
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def interrupted():
        measurements.append(problem.values())
        result = evaluate()
        if len(measurements) == 2:
            clock[0] = 2.0
            if stop == "error":
                raise RuntimeError("求值中断")
        return result

    monkeypatch.setattr(problem.evaluator, "evaluate", interrupted)
    if stop == "error":
        with pytest.raises(RuntimeError, match="求值中断"):
            rtc_optim.local(problem, iterations=5)
    else:
        result = rtc_optim.local(problem, iterations=5, deadline=1.0 if stop == "timeout" else None,
                                 should_cancel=lambda: stop == "cancel" and len(measurements) >= 2)
        assert result["stopped_by"] == stop
    assert len(measurements) == 2
    np.testing.assert_array_equal(problem.values(), initial)


@pytest.mark.parametrize("achieved", [False, True])
def test_disagreed_confirmation_returns_to_design_even_at_the_round_limit(achieved):
    rows = [_round(0), _round(1, achieved=not achieved),
            {**_round(2, achieved=achieved), "tier": ledger.CONFIRM, "confirm_of": 0, "agrees": False, "backend": "zos"}]
    stop = {"rel": 0.01, "max_rounds": 1, "tiers": ["local"]}
    rounds = ledger.effective(rows)
    assert ledger.confirmation_failure(rounds)["round"] == 0
    decision = ledger.decide(rounds, stop)
    assert decision["status"] == "continue" and decision["confirm_failed"] == 0
    assert ledger.confirmation_failure(rounds + [_round(3)]) is None
