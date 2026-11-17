"""rtc 与真实 OpticStudio 的对照里，录不成离线真值的那几项：exec 脚本与 worker 里的 ctx.fast()、Seidel 与近轴操作数、
评价函数向导写出的行、建不了的处方留在 OpticStudio、半口径无关的操作数。默认跳过：
SAOS_RUN_ZEMAX=1 uv run pytest tests/test_zemax_rtc.py -s

逐条光线、一阶量、材料折射率已录成真值，离线回放（tests/rtc/test_case_truth.py、tests/test_rtc_backend.py、
tests/rtc/test_material_golden.py）。实际瞄准时 OpticStudio 自己的瞄准收敛差 5.7e-8 mm（2026-10-26），容差按 1e-6 mm 放。
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from saos import prescription
from record_truth import doublet

pytestmark = [
    pytest.mark.zemax,
    pytest.mark.skipif(os.environ.get("SAOS_RUN_ZEMAX") != "1", reason="需要真实 OpticStudio，设置 SAOS_RUN_ZEMAX=1"),
]

@pytest.fixture(scope="module")
def zos():
    from saos.backend.zos import ZosBackend

    backend = ZosBackend()
    backend.start()
    yield backend
    backend.close()


CASES = Path(__file__).parent / "fixtures" / "cases"


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
    # OPDX 的参考（piston 与倾斜）怎么取还没对上：2026-11-14 实测差 0.0236 个波，见 TODO.md
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


@pytest.mark.parametrize("name", ["iris", "reflective"])
def test_the_semi_independent_operands_do_not_move_with_the_semi_diameters_on_opticstudio(zos, name):
    """rtc.SEMI_INDEPENDENT 在 OpticStudio 上也成立：非光阑面的半口径改成 0.5 倍并设浮动孔径，边缘光线被挡（渐晕码非 0），
    REAY 仍给它的坐标，13 个操作数与改之前相同（2026-11-14 首次实测：iris、reflective、sequential 在 0.5/1.5/3 倍、
    有无浮动孔径下都在 1e-12 以内）。"""
    from saos.backend import rtc

    def values():
        n = zos.system.LDE.NumberOfSurfaces - 1
        calls = {"EFFL": (0, 0, []), "EXPP": (0, 0, []), "EXPD": (0, 0, []), "INDX": (1, 0, []), "SSAG": (1, 0, [1.0, 0.5]),
                 "SPHA": (0, 0, []), "COMA": (0, 0, []), "ASTI": (0, 0, []), "FCUR": (0, 0, []),
                 "REAY": (n, 0, [0, 1, 0, 1]), "REAX": (n, 0, [1, 0, 0.3, 0]), "REAC": (n, 0, [0, 1, 0, 0.5])}
        assert set(calls) | {"CONF"} == rtc.SEMI_INDEPENDENT
        return {op: zos.operand(op, *args) for op, args in calls.items()}

    zos.load(CASES / f"{name}.zmx")
    before = values()
    lde = zos.system.LDE
    floating = zos.zosapi().Editors.LDE.SurfaceApertureTypes.FloatingAperture
    for i in range(1, lde.NumberOfSurfaces - 1):
        row = lde.GetSurfaceAt(i)
        if not row.IsStop:
            row.SemiDiameter = float(row.SemiDiameter) * 0.5
            row.ApertureData.ChangeApertureTypeSettings(row.ApertureData.CreateApertureTypeSettings(floating))
    rim = zos.trace_batch(np.array([[1, 0, 1, 0, 1.0]]), lde.NumberOfSurfaces - 1)
    assert rim.error[0] == 0 and rim.vignette[0] != 0
    after = values()
    for op, value in before.items():
        assert after[op] == pytest.approx(value, rel=1e-12, abs=1e-12), op
