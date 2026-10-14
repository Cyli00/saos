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
def ui_pid():
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
    time.sleep(5)  # 启动画面关闭、功能区可用
    yield proc.pid
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)


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


def test_explicit_debugging_tools(ui_pid, tmp_path):
    env = {**os.environ, "SAOS_HOME": str(tmp_path / "home"), "SAOS_BACKEND": "zos", "SAOS_UI_PID": str(ui_pid)}
    started = ok(saos("daemon", "start", "--mode", "extension", env=env))
    try:
        ui = started["opticstudio"]["extension_ui"]
        assert ui["pid"] == ui_pid and ui["auto_close"] == "Off"
        # 连接后按规则打开 Lens Data 和默认像质分析
        assert started["opticstudio"]["windows"]["watch"]["analysis"] == "StandardSpot"
        assert any(w["type"] == "StandardSpot" for w in started["opticstudio"]["windows"]["windows"])

        job = ok(saos("job", "create", "--name", "explicit", env=env))["job"]
        ok(saos("lens", "import", "--job", job, SAMPLE, "--as", "start.zmx", env=env))
        ok(saos("lens", "load", "--job", job, "start.zmx", env=env))
        view = ok(saos("lens", "view", "--job", job, "--viewer", "cross-section", "--analysis", "FftMtf", "--set", "MaximumFrequency=50", env=env))
        types = {w["type"] for w in view["windows"]}
        assert {"Draw2D", "FftMtf"} <= types and view["watch"]["viewer"] == "cross-section"
        assert ok(saos("lens", "view", "--job", job, env=env))["watch"]["analysis"] == "FftMtf"  # 不指定时沿用上次的选择
        assert saos("lens", "view", "--job", job, "--analysis", "NoSuch", env=env)["error"]["code"] == ErrorCode.BAD_ARGS

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
        ok(saos("lens", "load", "--job", job, "start.zmx", env=env))
    finally:
        ok(saos("daemon", "stop", "--discard", env=env))
