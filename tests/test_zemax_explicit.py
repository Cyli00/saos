"""显性调试工具的真实回归：自己启动一个 OpticStudio 界面实例，通过 Interactive Extension 连接。

默认跳过。运行：SAOS_RUN_ZEMAX=1 SAOS_RUN_EXPLICIT=1 uv run pytest tests/test_zemax_explicit.py -s
会在桌面上弹出一个 OpticStudio 窗口，结束后关闭；用 SAOS_UI_PID 只连这个实例，不碰用户已打开的窗口。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import zemax_ui
from saos import config, native_ui
from saos.protocol import ErrorCode

pytestmark = [
    pytest.mark.zemax,
    pytest.mark.skipif(
        os.environ.get("SAOS_RUN_ZEMAX") != "1" or os.environ.get("SAOS_RUN_EXPLICIT") != "1",
        reason="需要真实 OpticStudio 界面，设置 SAOS_RUN_ZEMAX=1 和 SAOS_RUN_EXPLICIT=1",
    ),
]

SAMPLE = Path.home() / "Documents/Zemax/Samples/Sequential/Objectives/Cooke 40 degree field.zmx"


def saos(*args, env):
    proc = subprocess.run([sys.executable, "-m", "saos", *map(str, args)], capture_output=True, env=env, timeout=300)
    out = json.loads(proc.stdout.decode("utf-8"))
    print(f"saos {' '.join(map(str, args))} -> {'ok' if out['ok'] else out['error']['code']}")
    return out


def ok(out):
    assert out["ok"], out
    return out["result"]


@pytest.fixture
def ui_pid(tmp_path):
    exe = Path(config.opticstudio_dir()) / "OpticStudio.exe"
    proc = subprocess.Popen([str(exe)])
    suffix = native_ui._main_title_suffix()
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if any(w["pid"] == proc.pid for w in native_ui._top_windows(lambda t, v: v and suffix in t)):
            break
        time.sleep(1)
    else:
        proc.kill()
        pytest.fail("OpticStudio 界面没有在 90 s 内出现")
    # 自动保存恢复提示会在启动后几秒才弹出，有时还跟着错误消息框，都会挡住分析计算。
    # 观察到连续 8 s 没有阻塞对话框再开始（不删除用户的恢复文件）
    quiet_since = time.monotonic()
    deadline = time.monotonic() + 60
    while time.monotonic() - quiet_since < 8 and time.monotonic() < deadline:
        for w in native_ui._top_windows(lambda t, v: v and bool(t.strip())):
            if w["pid"] == proc.pid and w["title"] in native_ui.blocking_dialogs(proc.pid):
                zemax_ui.close_dialog(w["hwnd"])
                quiet_since = time.monotonic()
        time.sleep(0.5)
    yield proc.pid
    # 强制结束会留下自动保存的恢复文件，下次启动弹出模态框；先温和关闭，失败时只清理本测试的恢复记录
    zemax_ui.close_opticstudio(proc.pid)
    zemax_ui.clean_recovered(tmp_path)


PROBE = """
def run(ctx):
    # 窗口里已打开的 FftMtf 与新算一次的结果一致，说明改动后窗口被刷新了
    shown = None
    for i in range(1, ctx.system.Analyses.NumberOfAnalyses + 1):
        a = ctx.system.Analyses.Get_AnalysisAtIndex(i)
        if str(a.AnalysisType) == "FftMtf":
            shown = a.GetResults().GetDataSeries(0).YData.Data[20, 0]
    fresh = ctx.backend.analysis("FftMtf", {"MaximumFrequency": 50}, ctx.path("probe.txt"))["series"][0]["y"]["Tangential"][20]
    return {"shown": float(shown), "fresh": fresh}
"""


FOLD = """
def run(ctx):
    # 在第 1 面前插入绕 X 轴倾斜 10° 的坐标断点，系统变为非轴对称
    lde = ctx.zosapi.Editors.LDE
    row = ctx.system.LDE.InsertNewSurfaceAt(1)
    row.ChangeType(row.GetSurfaceTypeSettings(lde.SurfaceType.CoordinateBreak))
    row.GetSurfaceCell(lde.SurfaceColumn.Par3).DoubleValue = 10.0
    ctx.mark_dirty()
    return {"non_axial": bool(ctx.system.IsNonAxial)}
"""


def test_explicit_debugging_tools(ui_pid, tmp_path):
    env = {**os.environ, "SAOS_HOME": str(tmp_path / "home"), "SAOS_BACKEND": "zos", "SAOS_UI_PID": str(ui_pid)}
    started = ok(saos("daemon", "start", "--mode", "extension", env=env))
    try:
        ui = started["opticstudio"]["extension_ui"]
        assert ui["pid"] == ui_pid and ui["auto_close"] == "Off"
        # 连接后打开 Lens Data 和布局窗口，默认不开像质分析
        shown = started["opticstudio"]["windows"]
        assert shown["watch"]["analysis"] is None and shown["layout"]["viewer"] in ("cross-section", "3d")
        assert {w["type"] for w in shown["windows"]} & {"Draw2D", "Draw3D"}

        job = ok(saos("job", "create", "--name", "explicit", env=env))["job"]
        ok(saos("lens", "import", "--job", job, SAMPLE, "--as", "start.zmx", env=env))
        ok(saos("lens", "load", "--job", job, "start.zmx", env=env))
        view = ok(saos("lens", "view", "--job", job, "--viewer", "cross-section", "--analysis", "FftMtf", "--set", "MaximumFrequency=50", env=env))
        types = {w["type"] for w in view["windows"]}
        assert {"Draw2D", "FftMtf"} <= types and view["watch"]["viewer"] == "cross-section"
        assert view["update_mode"] == "EditorsOnly"
        assert ok(saos("lens", "view", "--job", job, env=env))["watch"]["analysis"] == "FftMtf"  # 不指定时沿用上次的选择
        assert saos("lens", "view", "--job", job, "--analysis", "NoSuch", env=env)["error"]["code"] == ErrorCode.BAD_ARGS

        # 折转后的系统画不了剖面图：关掉剖面图窗口，改开 3D 视图
        fold = tmp_path / "fold.py"
        fold.write_text(FOLD, encoding="utf-8")
        ok(saos("exec", fold, "--job", job, env=env))
        folded = ok(saos("lens", "view", "--job", job, env=env))
        assert folded["layout"]["viewer"] == "3d" and folded["layout"]["fallback_reason"], folded["layout"]
        assert "Draw3D" in {w["type"] for w in folded["windows"]} and "Draw2D" not in {w["type"] for w in folded["windows"]}
        ok(saos("lens", "load", "--job", job, "start.zmx", "--discard", env=env))
        assert ok(saos("lens", "view", "--job", job, "--viewer", "cross-section", env=env))["layout"]["viewer"] == "cross-section"

        # 改动镜头后窗口刷新
        surfaces = ok(saos("lens", "surfaces", "--job", job, env=env))["surfaces"]
        ok(saos("lens", "set", "--job", job, "--surface", 1, "--param", "radius", "--value", surfaces[1]["radius"] * 1.05, env=env))
        script = tmp_path / "probe.py"
        script.write_text(PROBE, encoding="utf-8")
        probe = ok(saos("exec", script, "--job", job, env=env))["result"]
        assert probe["shown"] == pytest.approx(probe["fresh"], rel=1e-9)

        # 窗口截图
        shots = ok(saos("lens", "screenshot", "--job", job, "--label", "after-set", env=env))["files"]
        assert shots
        for rel in shots:
            assert (tmp_path / "home" / "jobs" / job / rel).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        assert saos("lens", "screenshot", "--job", job, "--label", "bad label", env=env)["error"]["code"] == ErrorCode.BAD_ARGS

        # 有未保存修改时不能换镜头
        assert saos("lens", "load", "--job", job, "start.zmx", env=env)["error"]["code"] == ErrorCode.UNSAVED_CHANGES
        ok(saos("lens", "save", "--job", job, "--as", "r001.zmx", env=env))
        # 分析窗口会让 NeedsSave 置位；换镜头前窗口内容先存成副本，不阻断
        loaded = ok(saos("lens", "load", "--job", job, "start.zmx", env=env))
        lenses = tmp_path / "home" / "jobs" / job / "lenses"
        assert loaded["captured"] is None or (lenses / loaded["captured"]).is_file()
        ok(saos("lens", "save", "--job", job, "--no-verify", env=env))

        # 窗口里有改动时换镜头：--discard 只放弃 agent 自己的修改，窗口内容仍先存成副本，副本就是改动后的内容
        edited = surfaces[2]["thickness"] + 0.5
        ok(saos("lens", "set", "--job", job, "--surface", 2, "--param", "thickness", "--value", edited, env=env))
        replaced = ok(saos("lens", "load", "--job", job, "start.zmx", "--discard", env=env))
        assert replaced["captured"] and (lenses / replaced["captured"]).is_file()
        ok(saos("lens", "load", "--job", job, replaced["captured"], "--discard", env=env))
        assert ok(saos("lens", "surfaces", "--job", job, env=env))["surfaces"][2]["thickness"] == pytest.approx(edited)

        # check：未确认的需求也能逐项取值，证据写进 evaluations/
        demand_file = tmp_path / "home" / "jobs" / job / "sources" / "demand.yaml"
        demand_file.parent.mkdir(exist_ok=True)
        demand_file.write_text(json.dumps({"schema": 2, "summary": "track and spot. 发现任何这个demand可能存在的问题，要大胆质疑和询问。", "objectives": [
            {"id": "track", "description": "total track", "check": {"operand": {"type": "TOTR"}}, "origin": "user", "must": {"max": 1000.0}},
            {"id": "spot", "description": "on-axis RMS spot radius", "check": {"analysis": {"type": "StandardSpot", "path": "spot.rms_radius[0][1]"}}, "origin": "user", "must": {"max": 1000.0}}]}), encoding="utf-8")
        checked = ok(saos("check", "--job", job, env=env))
        assert checked["achieved"] and all(item["value"] > 0 for item in checked["items"])
        assert (tmp_path / "home" / "jobs" / job / checked["evidence_file"]).is_file()

        # capture 与 unload
        assert ok(saos("lens", "capture", "--job", job, "--as", "window.zmx", env=env))["file"] == "window.zmx"
        assert (lenses / "window.zmx").is_file()
        assert ok(saos("lens", "unload", "--job", job, env=env))["unloaded"]["file"] == "window.zmx"
    finally:
        ok(saos("daemon", "stop", "--discard", env=env))
