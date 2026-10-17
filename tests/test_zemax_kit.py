"""kit、worker 池和 saos scan 在真实 OpticStudio 2024 R1 上的回归。默认跳过：SAOS_RUN_ZEMAX=1 uv run pytest tests/test_zemax_kit.py -s

kit.trace 的落点与 REAX/REAY 操作数逐条一致；kit.spot 的 RMS 半径与 RSCE（高斯求积）相差不到 1%；
扫描在两个 standalone worker 上逐点求解 EFFL，并按需求单的判定实测。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from saos import demand, jobs
from saos.daemon import Daemon
from saos.scripting import ScriptContext

pytestmark = [
    pytest.mark.zemax,
    pytest.mark.skipif(os.environ.get("SAOS_RUN_ZEMAX") != "1", reason="需要真实 OpticStudio，设置 SAOS_RUN_ZEMAX=1"),
]

SAMPLE = Path.home() / "Documents/Zemax/Samples/Sequential/Objectives/Cooke 40 degree field.zmx"
SPOT_CHECK = '''
def run(ctx):
    rows = ctx.memo("spot", lambda: ctx.spot([(0, 0), (0, 0.7), (0, 1)], grid=31))
    return 1000 * max(r["rms_radius"] for r in rows)
'''


@pytest.fixture
def daemon(saos_home, monkeypatch):
    monkeypatch.setenv("SAOS_BACKEND", "zos")
    from saos.backend.zos import ZosBackend

    backend = ZosBackend("standalone")
    d = Daemon(backend, backend.start(), "standalone")
    yield d
    d.pool.close()
    backend.close()


def test_kit_matches_operands_and_scan_runs_on_workers(daemon):
    job = jobs.create_job("kit")
    assert daemon.handle({"op": "lens.import", "args": {"job": job.id, "src": str(SAMPLE), "name": "cooke.zmx"}})["ok"]
    assert daemon.handle({"op": "lens.load", "args": {"job": job.id, "file": "cooke.zmx"}})["ok"]
    ctx = ScriptContext(daemon, job, {})
    image = ctx.surface(-1)
    rows = [(2, 0.0, 1.0, 0.0, 1.0), (2, 0.0, 1.0, 0.0, -1.0), (1, 0.0, 0.5, 0.3, 0.2)]
    rays = ctx.trace(rows)
    assert rays.ok.all()
    for k, (w, hx, hy, px, py) in enumerate(rows):
        assert rays.y[k] == pytest.approx(ctx.op("REAY", image, w, hx, hy, px, py), abs=1e-9)
        assert rays.x[k] == pytest.approx(ctx.op("REAX", image, w, hx, hy, px, py), abs=1e-9)
    spots = ctx.spot([(0, 0), (0, 0.7), (0, 1)], wave=2, grid=81)
    for row, hy in zip(spots, (0, 0.7, 1)):
        assert row["pass_rate"] == 100.0 and row["rms_radius"] == pytest.approx(ctx.op("RSCE", 10, 2, 0, hy), rel=0.01)
    # 最后一面半径由求解锁住 F 数：写不进去时报错，不静默留下别的值 / the last radius holds the F-number by a solve
    with pytest.raises(Exception, match="did not take"):
        ctx.set({"6.radius": -20.0})
    r5 = ctx.get("5.radius")
    with ctx.kept(["5.radius"]):
        ctx.set({"5.curvature": 1 / (r5 * 1.1)})
        assert ctx.get("5.radius") == pytest.approx(r5 * 1.1)
    assert ctx.get("5.radius") == pytest.approx(r5, rel=1e-14)  # 内部按曲率存，末位可能变 / stored as curvature, last bit may change
    daemon.loaded.dirty = False  # kept 写回的是原值 / kept wrote the original values back

    (job.root / "checks").mkdir(exist_ok=True)
    (job.root / "checks" / "spot.py").write_text(SPOT_CHECK, encoding="utf-8")
    line = demand.QUESTION_LINE["zh"]
    d = {"schema": 2, "constants": [{"id": "efl", "description": "EFL", "value": 50, "unit": "mm", "source": "user",
                                     "check": {"operand": {"type": "EFFL", "int2": 2}}, "goal": {"equals": 50.0, "tol": 0.01}}],
         "variables": [{"id": "radii", "description": "radii are free"}],
         "objectives": [{"id": "spot", "description": "worst RMS spot radius, um", "check": {"script": "checks/spot.py"},
                         "origin": "user", "must": {"max": 10.0}, "source": "user"}],
         "summary": f"测试。{line}"}
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(d, allow_unicode=True), encoding="utf-8")
    r3, t4, totr = ctx.get("3.radius"), ctx.get("4.thickness"), ctx.op("TOTR")
    plan = {"points": [{"3.radius": r3 * f} for f in (0.98, 1.0, 1.02)],
            "solve": {"var": "4.thickness", "lo": t4 - 2, "hi": t4 + 2, "target": {"operand": ["TOTR"]}, "value": totr + 0.5, "xtol": 1e-9},
            "operands": {"efl": ["EFFL", 0, 2], "totr": ["TOTR"]}, "workers": 2, "keep": 1}
    res = daemon.handle({"op": "scan.run", "args": {"job": job.id, "plan": plan}})
    assert res["ok"], res
    res = res["result"]
    print({k: res[k] for k in ("elapsed_s", "startup", "workers")})
    assert res["valid"] == 3 and res["errors"] == {}
    for row in res["top"]:
        assert row["solve"]["ok"] and row["solve"]["x"] == pytest.approx(t4 + 0.5, abs=1e-6)
        assert row["operands"]["totr"] == pytest.approx(totr + 0.5, abs=1e-6) and row["values"]["efl"] == pytest.approx(50.0, abs=1e-4)
    assert res["kept_lenses"] and (job.lenses_dir / res["kept_lenses"][0]).is_file()
    assert ctx.get("3.radius") == pytest.approx(r3, rel=1e-14) and ctx.get("4.thickness") == pytest.approx(t4, rel=1e-14)


def test_detached_scan_on_opticstudio_while_the_main_instance_answers(daemon):
    import time

    from saos import scan

    job = jobs.create_job("detach")
    assert daemon.handle({"op": "lens.import", "args": {"job": job.id, "src": str(SAMPLE), "name": "cooke.zmx"}})["ok"]
    assert daemon.handle({"op": "lens.load", "args": {"job": job.id, "file": "cooke.zmx"}})["ok"]
    line = demand.QUESTION_LINE["zh"]
    d = {"schema": 2, "constants": [{"id": "lens", "description": "sample", "value": "cooke", "source": "user"}],
         "variables": [{"id": "radii", "description": "radii are free"}],
         "objectives": [{"id": "spot", "description": "RSCE on axis, mm", "check": {"operand": {"type": "RSCE", "int1": 6, "int2": 2}},
                         "origin": "user", "must": {"max": 0.001}, "source": "user"}],
         "summary": f"测试。{line}"}
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(d, allow_unicode=True), encoding="utf-8")
    ctx = ScriptContext(daemon, job, {})
    r3 = ctx.get("3.radius")
    plan = {"vars": {"3.radius": [r3 * (1 + k / 200) for k in range(-10, 10)]}, "workers": 2, "keep": 1}
    started = daemon.handle({"op": "scan.run", "args": {"job": job.id, "plan": plan, "detach": True}})["result"]
    assert started["detached"]
    t0 = time.monotonic()
    info = daemon.handle({"op": "lens.info", "args": {"job": job.id}})
    print("lens.info during the scan", round(time.monotonic() - t0, 3), "s")
    assert info["ok"] and daemon.running_background()
    while daemon.running_background() and time.monotonic() - t0 < 300:
        time.sleep(0.5)
    done = scan.show(job)
    assert done["finished"] and done["valid"] == 20 and done["errors"] == {}
    print({k: done[k] for k in ("elapsed_s", "valid")}, done["best"]["set"])
