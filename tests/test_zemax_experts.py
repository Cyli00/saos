"""SAOS_RUN_ZEMAX=1 时在真实 OpticStudio 上验证 ctx.AsphereExpert / ctx.GlassExpert，只用 tests/fixtures/cases 的三个案例：
三个案例按处方重建后与 reference.json 一致；序列案例上测非球面排名与完整恢复（评价函数含边界操作数）、玻璃候选与两种部分色散、
把专利起点那样的模型玻璃换成在产牌号；rtc 算的玻璃坐标与 INDX 一致、engine="rtc" 的排名跑通并恢复镜头（ADR-0015）；
非序列与混合模式案例测两类 Expert 都拒绝。"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

import numpy as np
import pytest

from saos import glasscat, jobs
from saos.backend.zos import ZosBackend
from saos.ctx.experts.asphere import mfe_residual
from saos.daemon import Daemon
from saos.protocol import ErrorCode, SaosError
from saos.scripting import ScriptContext

from zemax_support import call, opticstudio_pids

pytestmark = [pytest.mark.zemax, pytest.mark.skipif(os.environ.get("SAOS_RUN_ZEMAX") != "1", reason="需要显式启用真实 OpticStudio 回归")]

CASES = Path(__file__).parent / "fixtures" / "cases"
REFERENCE = json.loads((CASES / "reference.json").read_text(encoding="utf-8"))["cases"]


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    patch = pytest.MonkeyPatch()
    patch.setenv("SAOS_HOME", str(tmp_path_factory.mktemp("home")))
    patch.setenv("SAOS_BACKEND", "zos")
    patch.setenv("SAOS_UI", "0")
    before = opticstudio_pids()
    backend = ZosBackend()
    daemon = None
    try:
        daemon = Daemon(backend, backend.start())
        yield daemon, jobs.create_job("experts")
    finally:
        if daemon is not None:
            daemon.pool.close()
        backend.close()
        deadline = time.monotonic() + 30
        while opticstudio_pids() - before and time.monotonic() < deadline:
            time.sleep(1)
        patch.undo()
        assert not opticstudio_pids() - before, "OpticStudio processes left behind by this test"


def build(session, name, edit=None):
    """按案例处方重建并加载，返回该任务的脚本上下文；edit 在建之前改处方副本。"""
    daemon, job = session
    p = json.loads((CASES / f"{name}.json").read_text(encoding="utf-8"))
    if edit:
        edit(p)
    result = call(daemon, "lens.build", job=job.id, prescription=p, file=f"{name}.zmx", overwrite=True, discard=True)
    assert result["ok"], result
    return ScriptContext(daemon, job, {})


def prepare(ctx):
    """序列案例的评价函数与变量：向导的 RMS 光斑，加一条满足中的 OPGT（边界操作数，贡献为 0），两片的外侧曲率可变。"""
    ctx.backend.merit_wizard({"rings": 3, "arms": 6})
    ctx.merit_add([{"id": "efl", "type": "EFFL", "params": [0, 2], "target": 100, "weight": 0},
                   {"type": "OPGT", "params": [{"ref": "efl"}], "target": 90, "weight": 1}])
    for key in ("1.radius", "3.radius"):
        ctx.solve(key, "variable")
    return ctx


def test_cases_rebuild_to_their_reference_values(session):
    for name in ("sequential", "mixed"):
        ctx = build(session, name)
        assert float(ctx.op("EFFL", 0, 0)) == pytest.approx(REFERENCE[name]["EFFL"], rel=1e-9)
    ctx = build(session, "non_sequential")
    ctx.nsc_trace(split=False, scatter=False, seed=1)
    detector = ctx.detector("detector")
    assert detector["hits"] == REFERENCE["non_sequential"]["detector_hits"]
    assert detector["power"] == pytest.approx(REFERENCE["non_sequential"]["detector_power_w"], rel=1e-9)


def test_asphere_ranking_reads_boundary_operands_and_restores_the_lens(session):
    ctx = prepare(build(session, "sequential"))
    # DMFS 等标记行读出的权重是 inf，不是操作数
    rows = [r for r in ctx.backend.merit_rows() if 0 < r["weight"] < math.inf]
    bound = next(i for i, r in enumerate(rows) if r["type"] == "OPGT")
    e = mfe_residual(ctx)
    # 满足中的边界行对残差的贡献是 0，按权重重算才会和原生 merit 对不上
    assert rows[bound]["contribution"] == 0 and e[bound] == 0
    assert float(np.linalg.norm(e)) == pytest.approx(ctx.backend.merit_value(), rel=1e-9)
    surfaces, merit, dirty = ctx.backend.surfaces(), ctx.backend.merit_value(), ctx._daemon.loaded.dirty

    result = ctx.AsphereExpert([1, 3], terms=2, max_seconds=300).rank()
    assert "stopped_by" not in result
    assert result["order"] and all(r["improvement_factor"] >= 0 for r in result["selected"])
    assert ctx.backend.surfaces() == surfaces
    assert ctx.backend.merit_value() == pytest.approx(merit, rel=1e-12)
    assert ctx._daemon.loaded.dirty == dirty


def test_glass_candidates_keep_the_lens_and_offer_catalog_partial_dispersion(session):
    ctx = prepare(build(session, "sequential"))
    catalog = ctx.backend.substitute_catalog(None)
    if not catalog:
        pytest.skip("SAOS_CDGM_INPROD is not installed in the Glasscat folder (library/glasscat/README.md)")
    waves, surfaces = ctx.backend.system_info()["wavelengths"], ctx.backend.surfaces()

    working = ctx.GlassExpert([1], catalogs=[catalog], max_seconds=600).candidates(1)
    catalog_partial = ctx.GlassExpert([1], catalogs=[catalog], partial="gF", max_seconds=600).candidates(1)
    for result in (working, catalog_partial):
        assert "stopped_by" not in result and result["candidates"]
    assert ctx.backend.system_info()["wavelengths"] == waves
    assert ctx.backend.surfaces() == surfaces
    # 同一片玻璃：n、V 都按工作波段，只有部分色散的定义不同
    a = {c["name"]: c for c in working["candidates"]}
    shared = [c for c in catalog_partial["candidates"] if c["name"] in a]
    assert shared
    for c in shared:
        assert c["n"] == pytest.approx(a[c["name"]]["n"], rel=1e-12) and c["v"] == pytest.approx(a[c["name"]]["v"], rel=1e-12)
        assert not math.isclose(c["partial"], a[c["name"]]["partial"], rel_tol=1e-3)


def test_model_glasses_of_a_patent_start_become_in_production_glasses(session):
    def as_patent_codes(p):
        # 专利玻璃代码转来的就是模型玻璃（saos.starts.seq）：这里用 N-BK7、N-SF5 的 nd/vd
        p["surfaces"][0]["material"] = {"model": {"nd": 1.5168, "vd": 64.17}}
        p["surfaces"][1]["material"] = {"model": {"nd": 1.67271, "vd": 32.25}}

    ctx = prepare(build(session, "sequential", as_patent_codes))
    catalog = ctx.backend.substitute_catalog(None)
    if not catalog:
        pytest.skip("SAOS_CDGM_INPROD is not installed in the Glasscat folder (library/glasscat/README.md)")
    result = ctx.GlassExpert("model", catalogs=[catalog], neighbors=3, single_optimization_pass=True, max_passes=1,
                             max_seconds=900).run()
    assert result["surfaces"] == [1, 2] and result["feasibility_completed"], result
    names = {g.name.upper() for g in glasscat.load_catalogs([catalog], ctx.backend._glass_dir())}
    for s in ctx.backend.surfaces()[1:3]:
        assert s["solves"]["material"] != "MaterialModel" and s["material"].upper() in names
    assert ctx._daemon.loaded.dirty


def test_rtc_catalog_indices_match_opticstudio_indx(session):
    from saos.backend.rtc import catalog_indices

    ctx = build(session, "sequential")
    waves = [w["um"] for w in ctx.backend.system_info()["wavelengths"]]
    env = ctx.system.SystemData.Environment
    names = ["N-BK7", "N-SF5", "F2", "N-LAK22"]
    computed, skipped = catalog_indices(names, ["SCHOTT"], ctx.backend._glass_dir(),
                                        (float(env.Temperature), float(env.Pressure), bool(env.AdjustIndexToEnvironment)),
                                        waves)
    assert not skipped and set(computed) == set(names)
    for name in names:
        ctx.set({"1.material": name})
        measured = [abs(float(ctx.op("INDX", 1, i))) for i in range(1, len(waves)+1)]
        # 准入路径的约定：与 INDX 差 ≤ 1e-8（ADR-0014、ADR-0015）
        assert np.allclose(computed[name], measured, rtol=0, atol=1e-8), (name, computed[name], measured)

    ctx = build(session, "sequential")
    surfaces = ctx.backend.surfaces()
    result = ctx.GlassExpert([1], catalogs=["SCHOTT"], max_seconds=600).candidates(1)
    assert "stopped_by" not in result and result["candidates"]
    assert result["coordinate_sources"]["rtc"] > 0
    assert ctx.backend.surfaces() == surfaces


def test_rtc_asphere_ranking_runs_on_the_sequential_case_and_restores_the_lens(session):
    ctx = prepare(build(session, "sequential"))
    surfaces, merit, dirty = ctx.backend.surfaces(), ctx.backend.merit_value(), ctx._daemon.loaded.dirty

    result = ctx.AsphereExpert([1, 3], terms=2, engine="rtc", max_seconds=300).rank()
    assert "stopped_by" not in result and result["engine"] == "rtc"
    # 3 视场 × 3 波长，7×7 光瞳里落在单位圆内的 29 条光线，每条 x、y 两行；渐晕光线会被剔除
    assert 0 < result["residual_rows"] <= 3*3*29*2
    assert sorted(result["order"]) == [1, 3]
    assert all(r["improvement_factor"] >= 0 for r in result["selected"])
    assert ctx.backend.surfaces() == surfaces
    assert ctx.backend.merit_value() == pytest.approx(merit, rel=1e-12)
    assert ctx._daemon.loaded.dirty == dirty


@pytest.mark.parametrize("name", ["non_sequential", "mixed"])
def test_experts_refuse_non_sequential_and_mixed_lenses(session, name):
    ctx = build(session, name)
    for run in (lambda: ctx.GlassExpert([1]).candidates(1), lambda: ctx.AsphereExpert([1]).rank()):
        with pytest.raises(SaosError) as err:
            run()
        assert err.value.code == ErrorCode.BACKEND_UNSUPPORTED


def test_surface_params_list_the_type_columns_that_ctx_get_reads(session):
    def asphere(p):
        p["surfaces"][0].update(type="EvenAspheric", params={"par2": 1.2e-5})

    ctx = build(session, "sequential", asphere)
    rows = ctx.surface_params(1)
    by_par = {r["par"]: r for r in rows}
    assert by_par[2]["name"] == "4th Order Term" and by_par[2]["value"] == pytest.approx(1.2e-5, rel=1e-15)
    for r in rows:
        if isinstance(r["value"], float):
            assert ctx.get(f"1.par{r['par']}") == r["value"]
