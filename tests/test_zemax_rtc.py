"""rtc 副本（ctx.fast()）与真实 OpticStudio 2024 R1 的逐条光线对照和速度对比。默认跳过：
SAOS_RUN_ZEMAX=1 uv run pytest tests/test_zemax_rtc.py -s

样品是一片双胶合加偶次非球面的 4 面小镜头（建模、追迹都在秒级以内），两种设置：光阑在第 1 面且不瞄准；光阑在镜后且
实际光线瞄准。2026-10-26 实测：不瞄准时 600 条光线位置差 ≤ 1.4e-14 mm；实际瞄准时差 5.7e-8 mm，来自 OpticStudio 自己的
瞄准收敛：px = 1 的光线落在光阑 4.537567644 mm 处，比光阑半径（瞄准目标）4.537567657 mm 短 2.8e-9（相对），rtc 落在
目标上，所以瞄准设置的容差放宽到 1e-6 mm。另设 SAOS_RTC_RECORD=1 时把第一种设置的镜头副本、describe_model 和 OpticStudio 追迹结果写进
tests/fixtures/rtc/，供不开 OpticStudio 的 test_rtc_backend.py 回归。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import numpy as np
import pytest

from case_variants import truth
from saos import fast as fast_mod
from saos import prescription
from saos.kit import KitMethods

pytestmark = [
    pytest.mark.zemax,
    pytest.mark.skipif(os.environ.get("SAOS_RUN_ZEMAX") != "1", reason="需要真实 OpticStudio，设置 SAOS_RUN_ZEMAX=1"),
]

FIXTURES = Path(__file__).parent / "fixtures" / "rtc"


def doublet(stop_behind: bool) -> dict:
    """f ≈ 100 mm 的双胶合（N-BK7 / N-SF2），第 3 面带 4 次非球面项；视场含 x 分量，检验径向归一化。"""
    lens = [
        {"radius": 61.47, "thickness": 6.0, "material": "N-BK7"},
        {"radius": -44.64, "thickness": 2.5, "material": "N-SF2"},
        {"type": "EvenAspheric", "radius": -129.94, "thickness": 5.0 if stop_behind else 94.0, "material": "", "params": {"par2": 2e-7}},
    ]
    if stop_behind:
        surfaces = [*lens, {"radius": "inf", "thickness": 89.0, "material": "", "stop": True}]
    else:
        surfaces = [{**lens[0], "stop": True}, *lens[1:]]
    return {
        "title": "rtc doublet (test fixture)",
        "aperture": {"type": "entrance_pupil_diameter", "value": 10.0},
        "fields": {"type": "angle", "y": [0.0, 3.5, 5.0], "x": [0.0, 0.0, 2.0]},
        "wavelengths": {"um": [0.4861, 0.5876, 0.6563], "weights": [1.0, 1.0, 1.0], "primary": 2},
        "ray_aiming": "real" if stop_behind else "off",
        "object": {"thickness": "inf"},
        "surfaces": surfaces,
    }


FIELDS = [(0.0, 0.0), (0.0, 0.5), (0.0, 1.0), (0.3, -0.7)]


@pytest.mark.parametrize("temperature,pressure,adjust", [(-40.0, 0.0, True), (60.0, .5, True), (20.0, 1.0, False)])
def test_recovered_material_changes_match_application_indx(zos, temperature, pressure, adjust):
    model = doublet(False)
    model["catalogs"] = ["SCHOTT", "OHARA"]
    model["environment"] = {"temperature": temperature, "pressure": pressure, "adjust_index": adjust}
    zos.build(prescription.normalize(model))
    ctx = Ctx(zos)
    fast = ctx.fast()
    for material in ("S-BSL7", {"nd": 1.637, "vd": 57.3, "dpgf": .027}):
        fast.set({"1.material": material})
        fast.push(ctx)
        for wave in (1, 2, 3):
            assert fast.backend._system(wave).items[0].n_after == pytest.approx(ctx.op("INDX", 1, wave), rel=0, abs=1e-8)
        rebuilt = ctx.fast()
        assert rebuilt.backend.lens.environment == fast.backend.lens.environment


class Ctx(KitMethods):
    """测试用的最小上下文：kit 方法直接作用在 ZosBackend 上。/ A minimal context: kit methods on a ZosBackend."""

    in_check = False

    def __init__(self, backend):
        self.backend = backend
        self.cache = {}

    def fast(self, freeze_solves=False):
        return fast_mod.from_context(self, freeze_solves)


@pytest.fixture(scope="module")
def zos():
    from saos.backend.zos import ZosBackend

    backend = ZosBackend()
    backend.start()
    yield backend
    backend.close()


def _spot_rms(ctx_like) -> list[float]:
    rows = ctx_like.spot(FIELDS, wave=[1, 2, 3], grid=21)
    return [r["rms_radius"] for r in rows]


@pytest.mark.parametrize("stop_behind", [False, True], ids=["stop-first-no-aiming", "stop-behind-real-aiming"])
def test_rtc_copy_matches_opticstudio_and_is_faster(zos, stop_behind):
    zos.build(prescription.normalize(doublet(stop_behind)))
    ctx = Ctx(zos)
    fast = ctx.fast()
    image = ctx.surface(-1)

    # 实际瞄准时以 OpticStudio 的瞄准收敛为限，见模块说明 / with real aiming OpticStudio's own aiming tolerance limits it
    tol_pos, tol_dir, tol_rms = (1e-6, 1e-7, 1e-5) if stop_behind else (1e-8, 1e-9, 1e-7)
    for to in (2, image):
        diff = fast.compare(ctx, FIELDS, wave=[1, 2, 3], grid=9, to=to)
        print(f"\n[{'B' if stop_behind else 'A'}] surface {to}: {diff}")
        assert diff["ok_one_side"] == 0 and diff["ok_rtc"] == diff["rays"]
        assert max(diff["max_dx"], diff["max_dy"], diff["max_dz"]) < tol_pos
        assert max(diff["max_dl"], diff["max_dm"], diff["max_dn"]) < tol_dir

    zos_rms, rtc_rms = _spot_rms(ctx), _spot_rms(fast)
    assert rtc_rms == pytest.approx(zos_rms, rel=tol_rms, abs=1e-10)

    # 在副本上改一组参数，写回 OpticStudio 后两边应仍一致 / change the copy, push it, both sides agree again
    fast.set({"2.radius": -46.0, "3.par2": 5e-7, "3.thickness": fast.get("3.thickness") - 0.5})
    pushed = fast.push(ctx)
    assert set(pushed) == {"2.radius", "3.par2", "3.thickness"}
    after = fast.compare(ctx, FIELDS, wave=[1, 2, 3], grid=9)
    assert after["ok_one_side"] == 0 and max(after["max_dx"], after["max_dy"]) < tol_pos

    # 速度：同一组 49 个曲率点，每点改参数再算 4 视场 × 3 波长的点列 / speed over the same 49-point radius scan
    radii = np.linspace(-50.0, -42.0, 49)
    timings = {}
    for name, target in (("opticstudio", ctx), ("rtc", fast)):
        with target.kept(["2.radius"]):
            started = time.perf_counter()
            for r in radii:
                target.set({"2.radius": float(r)})
                _spot_rms(target)
            timings[name] = time.perf_counter() - started
    print(f"[{'B' if stop_behind else 'A'}] 49-point scan, 12 spots/point, grid 21: "
          f"OpticStudio {timings['opticstudio']:.2f} s, rtc {timings['rtc']:.2f} s, "
          f"×{timings['opticstudio'] / timings['rtc']:.1f}")
    assert timings["rtc"] < timings["opticstudio"]


CASES = Path(__file__).parent / "fixtures" / "cases"


def _case(name: str) -> dict:
    return json.loads((CASES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["iris", "reflective"])
def test_case_copy_matches_opticstudio_ray_by_ray(zos, name):
    """iris（Float By Stop Size + 面孔径）与 reflective（两次反射、负厚度、中心遮挡、穿过主镜孔）：
    面 2 与像面逐条光线对照，并核对 EPDI、EFFL、WFNO 与自动半口径。"""
    model = _case(name)
    zos.build(prescription.normalize(model))
    ctx = Ctx(zos)
    fast = ctx.fast()
    image = ctx.surface(-1)
    fields = fast_mod.field_points(fast.backend.lens)
    tol = 1e-6 if model["ray_aiming"] == "real" else 1e-9
    reference = truth(name)

    for to in (2, image):
        diff = fast.compare(ctx, fields, wave=[1, 2, 3], grid=7, to=to)
        print(f"\n[{name}] surface {to}: {diff}")
        assert diff["ok_one_side"] == 0
        assert max(diff["max_dx"], diff["max_dy"], diff["max_dz"]) < tol

    for operand in ("EPDI", "EFFL", "WFNO"):
        got = fast.backend.operand(operand, 0, 0, [])
        want = ctx.op(operand, 0, 0)
        assert got == pytest.approx(want, rel=1e-6), operand
        assert got == pytest.approx(reference["operands"][operand], rel=1e-6), operand


IN_DAEMON = '''
def on_worker(w, radius):
    fast = w.fast()
    fast.set({"2.radius": radius})
    fast.push(w)
    rtc = fast.spot([(0, 0), (0, 1), (0.3, -0.7)], wave=[1, 2, 3], grid=11)
    zos = w.spot([(0, 0), (0, 1), (0.3, -0.7)], wave=[1, 2, 3], grid=11)
    return {"built": fast.agreement, "rtc": [r["rms_radius"] for r in rtc], "zos": [r["rms_radius"] for r in zos]}

def run(ctx):
    fast = ctx.fast()
    rtc = fast.spot([(0, 0), (0, 1), (0.3, -0.7)], wave=[1, 2, 3], grid=11)
    zos = ctx.spot([(0, 0), (0, 1), (0.3, -0.7)], wave=[1, 2, 3], grid=11)
    mapped = ctx.map("on_worker", [-46.0, -43.0], workers=1)
    return {"built": fast.agreement, "rtc": [r["rms_radius"] for r in rtc], "zos": [r["rms_radius"] for r in zos],
            "mapped": mapped["results"], "errors": mapped["errors"]}
'''


def test_fast_copy_inside_the_daemon_and_on_a_worker(zos, saos_home, monkeypatch):
    """真实路径：saos exec 的脚本里调 ctx.fast()，ctx.map 的 worker 进程里调 w.fast()，各自与同一进程的 OpticStudio 对照。
    The real paths: ctx.fast() in a saos exec script and w.fast() in a ctx.map worker, each against its own OpticStudio."""
    from saos import jobs
    from saos.daemon import Daemon

    monkeypatch.setenv("SAOS_BACKEND", "zos")
    daemon = Daemon(zos, {})
    try:
        job = jobs.create_job("rtc")
        built = daemon.handle({"op": "lens.build", "args": {"job": job.id, "prescription": doublet(True), "file": "doublet.zmx"}})
        assert built["ok"], built
        out = daemon.handle({"op": "script.exec", "args": {"job": job.id, "name": "fast", "source": IN_DAEMON, "args": {}}})
        assert out["ok"], out
        r = out["result"]["result"]
        print(f"\n[daemon] built {r['built']}\n[daemon] rtc {r['rtc']}\n[daemon] zos {r['zos']}")
        assert r["rtc"] == pytest.approx(r["zos"], rel=1e-5) and r["errors"] == {}
        for item in r["mapped"]:
            print(f"[worker] built max_dx {item['built']['max_dx']:.2e}, rtc {item['rtc']}, zos {item['zos']}")
            assert item["rtc"] == pytest.approx(item["zos"], rel=1e-5)
    finally:
        daemon.pool.close()


def test_seidel_paraxial_and_distortion_operands_match_opticstudio(zos):
    """逐面 Seidel、DIST 总畸变、PARY/PATY、PETC 与 OpticStudio 同值（2026-11-10 实测：
    逐面 1.3e-15 波，PARY 在 0.6 Hy 处差 8.6e-3 mm 的旧口径已改为先取实际视场角）。"""
    from saos.backend.rtc import RtcBackend

    zos.load(CASES / "sequential.zmx")
    rtc = RtcBackend(CASES / "sequential.zmx", standalone=True)
    try:
        for wave in (1, 2, 3):
            mine, theirs = rtc.seidel(wave), zos.seidel(wave)
            assert [r["surface"] for r in mine["surfaces"]] == [r["surface"] for r in theirs["surfaces"]]
            for a, b in zip(mine["surfaces"], theirs["surfaces"]):
                for column in ("W040", "W131", "W222", "W220P", "W311"):
                    assert a[column] == pytest.approx(b[column], abs=1e-9), (wave, a["surface"], column)
            for column in ("W040", "W131", "W222", "W220P", "W311"):
                assert mine["totals"][column] == pytest.approx(theirs["totals"][column], abs=1e-9), (wave, column)
        for surface in (1, 2, 3, 4):
            for hy in (0.0, 0.6, 1.0):
                for py in (-1.0, -0.8, 0.0, 0.8, 1.0):
                    data = [0.0, hy, 0.0, py]
                    for operand in ("PARY", "PATY"):
                        assert rtc.operand(operand, surface, 2, data) == pytest.approx(
                            zos.operand(operand, surface, 2, data), abs=1e-9), (operand, surface, hy, py)
        for wave in (1, 2, 3):
            assert rtc.operand("DIST", 0, wave, []) == pytest.approx(zos.operand("DIST", 0, wave, []), abs=1e-9)
            assert rtc.operand("PETC", 0, wave, []) == pytest.approx(zos.operand("PETC", 0, wave, []), rel=1e-12)
    finally:
        rtc.close()


def test_wizard_reference_writes_the_operand_opticstudio_does(zos):
    """向导的 reference 标签与 OpticStudio 24.1 写出的操作数一致：Centroid→OPDX、Chief Ray→OPDM、
    Unreferenced→OPDC；rtc 不再接受 OpticStudio 没有的 Mean。只比操作数种类，不比行数：原生向导对轴上视场
    只写 3 个光瞳点，rtc 写 9 个，这个结构差异另记 TODO。"""
    from saos.backend.rtc import RtcBackend
    from saos.protocol import ErrorCode, SaosError

    pairs = [("Wavefront", "Centroid", "OPDX"), ("Wavefront", "Chief Ray", "OPDM"), ("Wavefront", "Unreferenced", "OPDC"),
             ("Spot Radius", "Centroid", "TRAC"), ("Spot Radius", "Chief Ray", "TRAR")]
    zos.load(CASES / "sequential.zmx")
    rtc = RtcBackend(CASES / "sequential.zmx", standalone=True)
    try:
        for data, reference, expected in pairs:
            zos.merit_clear()
            rtc.merit_clear()
            settings = {"data": data, "reference": reference, "rings": 3, "arms": 6}
            zos.merit_wizard(settings)
            rtc.merit_wizard(settings)
            assert {r["type"] for r in zos.merit_rows() if r["type"] not in ("DMFS", "BLNK")} == {expected}
            assert {r.type for r in rtc.merit if r.type not in ("DMFS", "BLNK")} == {expected}
        with pytest.raises(SaosError) as err:
            rtc.merit_wizard({"data": "Wavefront", "reference": "Mean", "rings": 3, "arms": 6})
        assert err.value.code == ErrorCode.BAD_ARGS
    finally:
        rtc.close()


@pytest.mark.parametrize("data, reference", [
    # OPDX 的参考（piston 与倾斜）怎么取还没对上：2026-11-14 实测差 0.0236 个波，见 TODO.md / OPDX's reference is not reconciled yet
    pytest.param("Wavefront", "Centroid", marks=pytest.mark.xfail(strict=True, reason="OPDX reference not reconciled (TODO.md)")),
    ("Wavefront", "Chief Ray"), ("Wavefront", "Unreferenced"),
                                             ("Spot Radius", "Centroid"), ("Spot Radius", "Chief Ray")])
def test_opticstudio_wizard_rows_have_the_same_values_on_rtc(zos, tmp_path, data, reference):
    """OpticStudio 向导写出的行原样交给 rtc 求值，逐行同值：主光线参考的行和 OPD 的参考球取主波长的主光线，
    半光瞳的质心组按左右对称补全。行由 OpticStudio 存进 .zmx 再由 rtc 读入，所以比的是同一批行、同一组权重。"""
    from saos.backend.rtc import RtcBackend

    zos.load(CASES / "sequential.zmx")
    zos.merit_clear()
    zos.merit_wizard({"data": data, "reference": reference, "rings": 3, "arms": 6})
    theirs = zos.merit_rows()
    saved = tmp_path / "wizard.zmx"
    zos.export_native(saved)
    rtc = RtcBackend(saved, standalone=True)
    try:
        mine = rtc.merit_rows()
        assert [(r["row"], r["type"]) for r in mine] == [(r["row"], r["type"]) for r in theirs]
        kinds = {r["type"] for r in theirs} - {"DMFS"}
        assert len(kinds) == 1
        for a, b in zip(mine, theirs):
            if a["type"] != "DMFS":
                assert a["value"] == pytest.approx(b["value"], abs=1e-8), (a["row"], a["type"])
        assert rtc.merit_value() == pytest.approx(zos.merit_value(), rel=1e-8)
    finally:
        rtc.close()


def test_record_offline_fixture(zos):
    """SAOS_RTC_RECORD=1 时重写 tests/fixtures/rtc/ 的基准；否则跳过。/ Rewrite the offline fixture when asked."""
    if os.environ.get("SAOS_RTC_RECORD") != "1":
        pytest.skip("set SAOS_RTC_RECORD=1 to rewrite tests/fixtures/rtc/")
    zos.build(prescription.normalize(doublet(False)))
    ctx = Ctx(zos)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    model = zos.export_rtc(FIXTURES / "doublet.zmx")
    (FIXTURES / "doublet.ZDA").unlink(missing_ok=True)
    for local in ("glass_dir", "file"):
        model.pop(local, None)
    rows = [(w, hx, hy, px, py) for hx, hy in FIELDS for w in (1, 2, 3) for px, py in [(0.0, 0.0), *ctx_pupil()]]
    traces = {}
    for to in (2, ctx.surface(-1)):
        r = ctx.trace(rows, to)
        traces[str(to)] = {k: getattr(r, k).tolist() for k in ("error", "vignette", "x", "y", "z", "l", "m", "n")}
    data = {"source": "OpticStudio 24.1 ZOS-API standalone, tests/test_zemax_rtc.py::test_record_offline_fixture",
            "model": model, "rays": rows, "traces": traces, "spot_rms": _spot_rms(ctx)}
    (FIXTURES / "doublet.json").write_text(json.dumps(data, indent=1, allow_nan=True), encoding="utf-8")


def ctx_pupil():
    from saos.kit import pupil

    return pupil(7)


def test_a_prescription_rtc_cannot_build_is_built_on_opticstudio_and_stays_there(zos, saos_home, monkeypatch):
    """带偶次非球面反射镜的处方：daemon 的预检在临时 rtc 上建一遍发现建不了，就在 OpticStudio 上建，而不是报
    BACKEND_UNSUPPORTED；之后的命令留在 OpticStudio（backend_switch.stayed），数是 measured。"""
    from case_variants import _reflective_even_asphere, base

    from saos import jobs
    from saos.backend.rtc import RtcBackend
    from saos.daemon import Daemon

    monkeypatch.setenv("SAOS_BACKEND", "auto")
    raw = base("reflective")
    _reflective_even_asphere(raw)
    rtc = RtcBackend(None, standalone=True)
    daemon = Daemon(rtc, rtc.identity(), start_zos=lambda: (zos, zos.identity()))
    try:
        job = jobs.create_job("even-asphere-mirror")
        built = daemon.handle({"op": "lens.build", "args": {"job": job.id, "prescription": prescription.normalize(raw), "file": "refl.zmx"}})
        assert built["ok"], built
        switch = built["result"]["backend_switch"]
        assert switch["to"] == "zos" and "EVENASPH mirror" in switch["reason"]
        effl = daemon.handle({"op": "lens.operand", "args": {"job": job.id, "type": "EFFL"}})
        assert effl["ok"], effl
        result = effl["result"]
        assert result["computed_by"]["backend"] == "zos" and result["trust"] == "measured"
        assert "EVENASPH mirror" in result["backend_switch"]["stayed"]
    finally:
        daemon.pool.close()
