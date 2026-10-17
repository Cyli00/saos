"""通用工作流的真实回归：改镜头数据 → 取光学指标 → 分析 → 下一轮 → 达成或无法再优化 → HTML 报告。

默认跳过。运行：SAOS_RUN_ZEMAX=1 uv run pytest tests/test_zemax_workflow.py -s
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = [
    pytest.mark.zemax,
    pytest.mark.skipif(os.environ.get("SAOS_RUN_ZEMAX") != "1", reason="需要真实 OpticStudio，设置 SAOS_RUN_ZEMAX=1"),
]

SAMPLE = Path.home() / "Documents/Zemax/Samples/Sequential/Objectives/Cooke 40 degree field.zmx"

HAMMER = """
def run(ctx):
    # 全局层级：Hammer 优化跑几秒后中断（它本身会一直运行到人为中断）
    tool = ctx.system.Tools.OpenHammerOptimization()
    try:
        before = float(tool.InitialMeritFunction)
        tool.RunAndWaitWithTimeout(float(ctx.args.get("seconds", 5)))
        tool.Cancel()
        tool.WaitForCompletion()
        after = float(tool.CurrentMeritFunction)
    finally:
        tool.Close()
    ctx.mark_dirty()
    return {"initial_merit": before, "final_merit": after}
"""


def saos(*args, env):
    proc = subprocess.run([sys.executable, "-m", "saos", *map(str, args)], capture_output=True, env=env, timeout=600)
    out = json.loads(proc.stdout.decode("utf-8"))
    print(f"saos {' '.join(map(str, args))[:110]} -> {'ok' if out['ok'] else out['error']['code']}")
    return out


def ok(out):
    assert out["ok"], out
    return out["result"]


def write_demand(home, job, data, quote):
    (home / "jobs" / job / "sources" / "demand.yaml").write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    ok(saos("demand", "confirm", "--job", job, "--quote", quote, env={**os.environ, "SAOS_HOME": str(home), "SAOS_UI": "0"}))


def test_workflow_until_achieved_or_exhausted_with_reports(saos_home, tmp_path):
    env = {**os.environ, "SAOS_HOME": str(saos_home), "SAOS_BACKEND": "zos"}
    ok(saos("daemon", "start", env=env))
    try:
        job = ok(saos("job", "create", "--name", "cooke", env=env))["job"]
        ok(saos("lens", "import", "--job", job, SAMPLE, "--as", "sample.zmx", env=env))
        ok(saos("lens", "load", "--job", job, "sample.zmx", env=env))
        f0 = ok(saos("lens", "operand", "--job", job, "EFFL", env=env))["value"]
        spot0 = ok(saos("lens", "analysis", "--job", job, "StandardSpot", env=env))["spot"]["rms_radius"][0][1]

        # 改镜头数据：扰动两个曲率，存为起点
        surfaces = ok(saos("lens", "surfaces", "--job", job, env=env))["surfaces"]
        ok(saos("lens", "set", "--job", job, "--surface", 1, "--param", "radius", "--value", surfaces[1]["radius"] * 1.04, env=env))
        ok(saos("lens", "set", "--job", job, "--surface", 4, "--param", "radius", "--value", surfaces[4]["radius"] * 0.96, env=env))
        ok(saos("lens", "save", "--job", job, "--as", "start.zmx", env=env))

        write_demand(saos_home, job, {
            "schema": 2,
            "constants": [{"id": "efl", "description": "effective focal length", "check": {"operand": {"type": "EFFL"}},
                           "goal": {"equals": f0, "tol": 0.01 * f0}, "source": "user: keep the original focal length within 1 %"},
                          {"id": "system", "description": "the sample's fields and wavelengths", "value": "as in the sample", "source": "agent assumption: not discussed"}],
            "variables": [{"id": "radii_thickness", "description": "radii and thicknesses are free"}],
            "objectives": [
                {"id": "spot_axis", "description": "on-axis RMS spot radius (um), primary wave",
                 "check": {"analysis": {"type": "StandardSpot", "path": "spot.rms_radius[0][1]"}}, "origin": "user", "must": {"max": 1.5 * spot0},
                 "source": "user: no worse than 50 % above the original design"},
                {"id": "track", "description": "total track", "check": {"operand": {"type": "TOTR"}}, "origin": "agent"},
            ],
            "summary": "焦距保持原样（±1%），轴上像点尽量小，不比原设计差 50% 以上。发现任何这个demand可能存在的问题，要大胆质疑和询问。",
            "stop": {"rel": 0.01, "max_rounds": 8, "tiers": ["local", "variables", "global"]},
        }, "可以，按这个做")
        # 模型确认：没有确认过的建模说明不能记录轮次 / model gate: no rounds before an approved description
        assert saos("round", "--job", job, env=env)["error"]["code"] == "MODEL_NOT_APPROVED"
        notes = tmp_path / "notes.json"
        notes.write_text(json.dumps({"surfaces": {str(i): f"surface {i} of the sample" for i in range(len(surfaces))},
                                     "assumptions": ["sample lens"], "first_order": ["focal length as designed"]}), encoding="utf-8")
        described = ok(saos("lens", "describe", "--job", job, "--notes", notes, env=env))
        ok(saos("lens", "approve", "--job", job, "--model", described["folder"], "--quote", "模型没问题", env=env))
        r0 = ok(saos("round", "--job", job, "--note", "perturbed start", env=env))
        assert all(m["kind"] == "catalog" for m in r0["record"]["materials"]) and r0["record"]["model_glass"] == []
        assert r0["record"]["tier"] == "baseline" and not r0["record"]["achieved"]

        # 下一轮调试：放开曲率，评价函数为 RMS 光斑并约束焦距，局部优化
        ok(saos("opt", "vary", "--job", job, "--param", "radius", "--surfaces", "1-6", env=env))
        settings = tmp_path / "wizard.json"
        settings.write_text(json.dumps({"type": "RMS", "data": "Spot Radius", "reference": "Centroid"}), encoding="utf-8")
        ok(saos("opt", "merit", "--job", job, "wizard", "--file", settings, env=env))
        rows = tmp_path / "rows.json"
        rows.write_text(json.dumps([{"type": "EFFL", "int2": 2, "target": f0, "weight": 10}]), encoding="utf-8")
        ok(saos("opt", "merit", "--job", job, "add", "--file", rows, env=env))
        ok(saos("opt", "local", "--job", job, env=env))
        decision = ok(saos("round", "--job", job, "--tier", "local", "--note", "radii + DLS", env=env))["decision"]
        if decision["status"] != "achieved":
            ok(saos("opt", "vary", "--job", job, "--param", "thickness", "--surfaces", "1-5", env=env))
            ok(saos("opt", "local", "--job", job, env=env))
            decision = ok(saos("round", "--job", job, "--tier", "variables", "--note", "+ thickness", env=env))["decision"]
        assert decision["status"] == "achieved", decision
        rep = ok(saos("report", "--job", job, env=env))
        html = Path(rep["report"]).read_text(encoding="utf-8")
        assert rep["status"] == "achieved" and "达成" in html and "StandardSpot" in html and "EFFL" in html

        # 无法再优化：只放开曲率，总长 ≤ 1 mm 永远改变不了；逐级用完允许的手段后判停
        job2 = ok(saos("job", "create", "--name", "impossible", env=env))["job"]
        ok(saos("lens", "import", "--job", job2, saos_home / "jobs" / job / "lenses" / rep["deliver"], "--as", "start.zmx", env=env))
        ok(saos("lens", "load", "--job", job2, "start.zmx", "--discard", env=env))
        write_demand(saos_home, job2, {
            "schema": 2,
            "objectives": [{"id": "track", "description": "total track", "check": {"operand": {"type": "TOTR"}}, "origin": "user", "must": {"max": 1.0},
                            "source": "user: an unreachable track to force the exhausted ending"}],
            "summary": "总长做到 1 mm 以内。发现任何这个demand可能存在的问题，要大胆质疑和询问。",
            "stop": {"rel": 0.01, "max_rounds": 10, "tiers": ["local", "global"]},
        }, "试试看")
        described = ok(saos("lens", "describe", "--job", job2, "--notes", notes, env=env))
        ok(saos("lens", "approve", "--job", job2, "--model", described["folder"], "--quote", "同一个模型", env=env))
        ok(saos("round", "--job", job2, env=env))
        ok(saos("opt", "vary", "--job", job2, "--param", "radius", "--surfaces", "1-6", env=env))
        ok(saos("opt", "merit", "--job", job2, "wizard", "--file", settings, env=env))
        ok(saos("opt", "local", "--job", job2, env=env))
        d1 = ok(saos("round", "--job", job2, "--tier", "local", env=env))["decision"]
        assert d1["status"] == "continue" and d1["next_tier"] == "global"
        script = tmp_path / "hammer.py"
        script.write_text(HAMMER, encoding="utf-8")
        hammer = ok(saos("exec", script, "--job", job2, "--name", "hammer", "--arg", "seconds=5", env=env))["result"]
        assert hammer["final_merit"] <= hammer["initial_merit"] + 1e-12
        d2 = ok(saos("round", "--job", job2, "--tier", "global", "--note", "Hammer 5 s", env=env))["decision"]
        assert d2["status"] == "exhausted", d2
        assert saos("round", "--job", job2, "--tier", "local", env=env)["error"]["code"] == "BAD_ARGS"
        rep2 = ok(saos("report", "--job", job2, env=env))
        html2 = Path(rep2["report"]).read_text(encoding="utf-8")
        assert rep2["status"] == "exhausted" and "无法再优化" in html2 and "超出" in html2 and "scripts/" in html2
    finally:
        ok(saos("daemon", "stop", "--discard", env=env))
