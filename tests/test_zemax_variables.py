"""变量类型在真实 OpticStudio 2024 R1 上的回归：面型参数列 par<N>、模型玻璃、目录内替换、非轴对称判断。

默认跳过。运行：SAOS_RUN_ZEMAX=1 uv run pytest tests/test_zemax_variables.py -s
用 standalone 实例（不弹窗口），任务目录放在临时 SAOS_HOME 下。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from saos.protocol import ErrorCode

pytestmark = [
    pytest.mark.zemax,
    pytest.mark.skipif(os.environ.get("SAOS_RUN_ZEMAX") != "1", reason="需要真实 OpticStudio，设置 SAOS_RUN_ZEMAX=1"),
]

SAMPLE = Path.home() / "Documents/Zemax/Samples/Sequential/Objectives/Cooke 40 degree field.zmx"

ASPHERE = """
def run(ctx):
    # 第 1 面改成偶次非球面，读出各参数列的表头
    lde = ctx.zosapi.Editors.LDE
    row = ctx.system.LDE.GetSurfaceAt(1)
    row.ChangeType(row.GetSurfaceTypeSettings(lde.SurfaceType.EvenAspheric))
    ctx.mark_dirty()
    return {n: str(row.GetSurfaceCell(getattr(lde.SurfaceColumn, f"Par{n}")).Header) for n in range(1, 4)}
"""

FOLD = """
def run(ctx):
    before = bool(ctx.system.IsNonAxial)
    lde = ctx.zosapi.Editors.LDE
    row = ctx.system.LDE.InsertNewSurfaceAt(1)
    row.ChangeType(row.GetSurfaceTypeSettings(lde.SurfaceType.CoordinateBreak))
    row.GetSurfaceCell(lde.SurfaceColumn.Par3).DoubleValue = 10.0
    ctx.mark_dirty()
    return {"before": before, "after": bool(ctx.system.IsNonAxial), "mode": str(ctx.system.Mode)}
"""


def saos(*args, env):
    proc = subprocess.run([sys.executable, "-m", "saos", *map(str, args)], capture_output=True, env=env, timeout=300)
    out = json.loads(proc.stdout.decode("utf-8"))
    print(f"saos {' '.join(map(str, args))} -> {'ok' if out['ok'] else out['error']['code']}")
    return out


def ok(out):
    assert out["ok"], out
    return out["result"]


def test_variable_kinds(tmp_path):
    env = {**os.environ, "SAOS_HOME": str(tmp_path / "home"), "SAOS_BACKEND": "zos"}
    ok(saos("daemon", "start", env=env))
    try:
        job = ok(saos("job", "create", "--name", "vars", env=env))["job"]
        ok(saos("lens", "import", "--job", job, SAMPLE, "--as", "cooke.zmx", env=env))
        ok(saos("lens", "load", "--job", job, "cooke.zmx", env=env))
        surfaces = ok(saos("lens", "surfaces", "--job", job, env=env))["surfaces"]
        glass = next(s for s in surfaces if s["material"] and s["index"] > 0)

        # 模型玻璃：起点取目录玻璃的 nd、νd；--off 保留数值、停止变化
        varied = ok(saos("opt", "vary", "--job", job, "--param", "material", "--mode", "model", "--surfaces", glass["index"], env=env))
        assert list(varied["solves"].values()) == ["MaterialModel"]
        script = tmp_path / "model.py"
        script.write_text(f"def run(ctx):\n    m = ctx.system.LDE.GetSurfaceAt({glass['index']}).MaterialCell.GetSolveData()._S_MaterialModel\n"
                          "    return {'nd': m.IndexNd, 'vd': m.AbbeVd, 'vary': [m.VaryIndex, m.VaryAbbe]}\n", encoding="utf-8")
        model = ok(saos("exec", script, "--job", job, env=env))["result"]
        assert 1.4 < model["nd"] < 2.1 and 15 < model["vd"] < 100 and model["vary"] == [True, True], model
        ok(saos("opt", "vary", "--job", job, "--param", "material", "--surfaces", glass["index"], "--off", env=env))
        fixed = ok(saos("exec", script, "--job", job, env=env))["result"]
        assert fixed["nd"] == pytest.approx(model["nd"]) and fixed["vary"] == [False, False]
        sub = ok(saos("opt", "vary", "--job", job, "--param", "material", "--mode", "substitute", "--surfaces", glass["index"], env=env))
        assert list(sub["solves"].values()) == ["MaterialSubstitute"]
        air = next(s for s in surfaces if not s["material"] and 0 < s["index"] < len(surfaces) - 1)
        assert saos("opt", "vary", "--job", job, "--param", "material", "--mode", "model", "--surfaces", air["index"], env=env)["error"]["code"] == ErrorCode.BAD_ARGS

        # 面型参数列：偶次非球面的系数可以写入、读回、设为变量；没有的列报错
        script = tmp_path / "asphere.py"
        script.write_text(ASPHERE, encoding="utf-8")
        headers = ok(saos("exec", script, "--job", job, env=env))["result"]
        print("EvenAspheric headers:", headers)
        set_ = ok(saos("lens", "set", "--job", job, "--surface", 1, "--param", "par2", "--value", 1e-6, env=env))
        assert set_["after"] == pytest.approx(1e-6) and set_["before"] == 0.0
        assert ok(saos("opt", "vary", "--job", job, "--param", "par2", "--surfaces", 1, env=env))["solves"] == {"1": "Variable"}
        assert saos("lens", "set", "--job", job, "--surface", 2, "--param", "par2", "--value", 1, env=env)["error"]["code"] == ErrorCode.BAD_ARGS

        # 插入带倾斜的坐标断点后系统变为非轴对称，剖面图窗口据此改用 3D
        script = tmp_path / "fold.py"
        script.write_text(FOLD, encoding="utf-8")
        fold = ok(saos("exec", script, "--job", job, env=env))["result"]
        assert fold == {"before": False, "after": True, "mode": "Sequential"}, fold
    finally:
        ok(saos("daemon", "stop", "--discard", env=env))


WORK = """
def work(wctx, item):
    wctx.backend.set_surface(1, "radius", item["r"])
    return wctx.backend.operand("RSCE", 6, 0, [0.0, 1.0])
"""

ONE = """
def run(ctx):
    ctx.backend.set_surface(1, "radius", ctx.args["r"])
    value = ctx.backend.operand("RSCE", 6, 0, [0.0, 1.0])
    ctx.load("cooke.zmx")
    return value
"""

SCAN = """
def run(ctx):
    return ctx.parallel("explore/work.py", [{"r": r} for r in ctx.args["radii"]], workers=3)
"""


def test_parallel_matches_the_main_instance(tmp_path):
    """3 个 standalone worker 并发算出的 RSCE 与主实例逐个算的相同。"""
    env = {**os.environ, "SAOS_HOME": str(tmp_path / "home"), "SAOS_BACKEND": "zos"}
    ok(saos("daemon", "start", env=env))
    try:
        job = ok(saos("job", "create", "--name", "par", env=env))["job"]
        ok(saos("lens", "import", "--job", job, SAMPLE, "--as", "cooke.zmx", env=env))
        ok(saos("lens", "load", "--job", job, "cooke.zmx", env=env))
        r0 = ok(saos("lens", "surfaces", "--job", job, env=env))["surfaces"][1]["radius"]
        radii = [r0 * (1 + 0.002 * k) for k in range(30)]
        explore = tmp_path / "home" / "jobs" / job / "explore"
        explore.mkdir(parents=True)
        (explore / "work.py").write_text(WORK, encoding="utf-8")
        for name, text in (("scan.py", SCAN), ("one.py", ONE)):
            (tmp_path / name).write_text(text, encoding="utf-8")
        res = ok(saos("exec", tmp_path / "scan.py", "--job", job, "--arg", f"radii={json.dumps(radii)}", "--timeout", 600, env=env))["result"]
        print("workers:", res["workers"], "elapsed:", res["elapsed_s"])
        assert res["workers_available"] >= 1 and not res["errors"] and not res["not_run"]
        for k in (0, 17, 29):
            single = ok(saos("exec", tmp_path / "one.py", "--job", job, "--arg", f"r={radii[k]}", env=env))["result"]
            assert res["results"][k] == pytest.approx(single, rel=1e-12), (k, res["results"][k], single)
    finally:
        ok(saos("daemon", "stop", "--discard", env=env))
