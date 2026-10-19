"""saos ui：阶段推断、每轮 viz.json 导出与本机服务的访问控制（假后端，不启动浏览器）。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest
import yaml

from saos import demand, jobs, report
from saos.backend.fake import FakeBackend
from saos.daemon import Daemon
from saos.ui import section, server, stage

DEMAND = {
    "schema": 2,
    "constants": [{"id": "lens", "description": "the sample singlet", "value": "a.zmx", "source": "user"}],
    "variables": [{"id": "radii", "description": "both radii are free"}],
    "objectives": [{"id": "length", "description": "total track", "check": {"operand": {"type": "TOTR"}}, "origin": "user",
                    "must": {"max": 95.0}, "scale": 5.0, "source": "user"}],
    "summary": "总长不超过 95 mm。",
}
NOTES = {"surfaces": {"0": "object", "1": "lens front", "2": "lens back", "3": "image"}, "assumptions": ["test"], "first_order": ["f = 50 mm"]}


def call(daemon, op, **args):
    return daemon.handle({"op": op, "args": args})


def steps(job):
    snap = stage.snapshot(job)
    return snap["current"], {s["id"]: s["status"] for s in snap["steps"]}


@pytest.fixture
def flow(saos_home, fake_lens_file):
    backend = FakeBackend()
    daemon = Daemon(backend, backend.start(), "extension")
    job = jobs.create_job("ui")
    call(daemon, "lens.import", job=job.id, src=str(fake_lens_file), name="a.zmx")
    call(daemon, "lens.load", job=job.id, file="a.zmx")
    return daemon, job


def test_stage_follows_the_workflow_files(flow):
    daemon, job = flow
    assert steps(job) == ("interview", {"interview": "active", **dict.fromkeys(stage.STEPS[1:], "pending")})
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(DEMAND, allow_unicode=True), encoding="utf-8")
    current, status = steps(job)
    assert current == "demand_confirm" and status["interview"] == "done" and status["demand_confirm"] == "waiting"
    demand.confirm(job.root, "可以")
    assert steps(job)[0] == "modeling" and steps(job)[1]["modeling"] == "active"
    folder = call(daemon, "lens.describe", job=job.id, notes=NOTES)["result"]["folder"]
    snap = stage.snapshot(job)
    assert snap["current"] == "model_approve" and snap["steps"][3] == {"id": "model_approve", "status": "waiting", "detail": folder}
    assert call(daemon, "lens.approve", job=job.id, model=folder, quote="模型可以")["ok"]
    assert steps(job) == ("design", {**dict.fromkeys(stage.STEPS[:4], "done"), "design": "active", "deliver": "pending"})

    r0 = call(daemon, "round.record", job=job.id)["result"]
    assert r0["viz"]["ok"] and (job.root / r0["viz"]["file"]).is_file()
    snap = stage.snapshot(job)
    assert snap["rounds"][0]["viz"] and snap["steps"][4]["detail"]["rounds"] == 1 and snap["decision"]["status"] == "continue"
    # 需求改动后要重新确认：回到确认需求，带出原因 / a demand change goes back to confirmation
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump({**DEMAND, "objectives": [{**DEMAND["objectives"][0], "must": {"max": 96.0}}]},
                                                                     allow_unicode=True), encoding="utf-8")
    snap = stage.snapshot(job)
    assert snap["current"] == "demand_confirm" and snap["steps"][1]["detail"] == "reconfirm"
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(DEMAND, allow_unicode=True), encoding="utf-8")

    daemon.backend.lens["surfaces"][2]["thickness"] = 85.0
    r1 = call(daemon, "round.record", job=job.id, tier="local", viz=False)["result"]
    assert r1["decision"]["status"] == "achieved" and "viz" not in r1
    current, status = steps(job)
    assert current == "deliver" and status["design"] == "done" and status["deliver"] == "active"
    assert [r["viz"] for r in stage.snapshot(job)["rounds"]] == [True, False]
    report.write(job, lang="zh")
    assert steps(job)[1]["deliver"] == "done"
    assert not job.verify_seals()


def test_blocking_question_marks_the_current_step(flow):
    daemon, job = flow
    data = {**DEMAND, "open_questions": [{"question": "样品折射率？", "blocking": True}]}
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    demand.confirm(job.root, "可以")
    snap = stage.snapshot(job)
    assert snap["current"] == "modeling" and snap["steps"][2]["status"] == "blocked" and snap["blocking"] == ["样品折射率？"]


def test_viz_failure_never_undoes_the_round(flow, monkeypatch):
    daemon, job = flow
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(DEMAND, allow_unicode=True), encoding="utf-8")
    demand.confirm(job.root, "可以")
    folder = call(daemon, "lens.describe", job=job.id, notes=NOTES)["result"]["folder"]
    call(daemon, "lens.approve", job=job.id, model=folder, quote="可以")

    def broken(folder):
        raise RuntimeError("ray trace failed")

    monkeypatch.setattr(daemon.backend, "viz_data", broken)
    result = call(daemon, "round.record", job=job.id)["result"]
    assert result["record"]["round"] == 0 and not result["viz"]["ok"] and "ray trace failed" in result["viz"]["errors"]["viz"]
    saved = json.loads((job.root / result["viz"]["file"]).read_text(encoding="utf-8"))
    assert saved["ok"] is False and stage.snapshot(job)["rounds"][0]["viz"]


@pytest.fixture
def ui(flow):
    srv = server.UiServer()
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield flow, srv
    srv.shutdown()
    srv.server_close()


# 本机可能设了 HTTP_PROXY；访问 127.0.0.1 不走代理 / the machine may set HTTP_PROXY; talk to 127.0.0.1 directly
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(srv, path, token=True, host=None, data=None):
    port = srv.server_address[1]
    headers = {"X-Saos-Token": srv.token if token else "wrong"}
    if host:
        headers["Host"] = host
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, headers=headers)
    try:
        with DIRECT.open(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if path.startswith("/api/") else raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, (json.loads(raw) if path.startswith("/api/") else raw.decode("utf-8"))


def test_server_serves_state_and_viz_behind_the_token(ui):
    (daemon, job), srv = ui
    code, html = get(srv, "/")
    assert code == 200 and "app.js" in html
    assert get(srv, "/static/app.js")[0] == 200 and get(srv, "/static/../server.py")[0] == 404
    assert get(srv, f"/api/state?job={job.id}", token=False)[0] == 403
    assert get(srv, f"/api/state?job={job.id}", host="evil.example:80")[0] == 403
    code, msg = get(srv, f"/api/state?job={job.id}")
    assert code == 200 and msg["ok"] and msg["result"]["current"] == "interview"

    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(DEMAND, allow_unicode=True), encoding="utf-8")
    demand.confirm(job.root, "可以")
    folder = call(daemon, "lens.describe", job=job.id, notes=NOTES)["result"]["folder"]
    call(daemon, "lens.approve", job=job.id, model=folder, quote="可以")
    call(daemon, "round.record", job=job.id)
    msg = get(srv, f"/api/viz?job={job.id}&round=0")[1]
    assert msg["ok"] and msg["result"]["surfaces"] and msg["result"]["spot"]["fields"]
    assert get(srv, f"/api/viz?job={job.id}&round=7")[1]["error"]["code"] == "BAD_ARGS"
    # 后台进程没在跑：窗口控制直接拒绝，不会去连接 / no daemon: window controls are refused
    refused = get(srv, f"/api/view?job={job.id}", data={"analysis": "StandardSpot"})[1]
    assert refused["error"]["code"] == "DAEMON_NOT_RUNNING"


def test_window_controls_only_forward_whitelisted_view_ops(ui, monkeypatch):
    (daemon, job), srv = ui
    monkeypatch.setattr(stage, "_daemon", lambda: {"running": True, "mode": "extension"})
    sent = []
    monkeypatch.setattr(server.client, "request", lambda op, args, **kw: sent.append((op, args)) or {"done": op})
    assert get(srv, f"/api/view?job={job.id}", data={"analysis": "FftMtf"})[1]["result"] == {"done": "lens.view"}
    assert get(srv, f"/api/view?job={job.id}", data={"viewer": "3d"})[1]["ok"]
    assert get(srv, f"/api/view?job={job.id}", data={"analysis": "../../evil"})[1]["error"]["code"] == "BAD_ARGS"
    assert get(srv, f"/api/view?job={job.id}", data={"viewer": "x"})[1]["error"]["code"] == "BAD_ARGS"
    assert sent == [("lens.view", {"job": job.id, "analysis": "FftMtf"}), ("lens.view", {"job": job.id, "viewer": "3d"})]
    monkeypatch.setattr(stage, "_daemon", lambda: {"running": True, "mode": "standalone"})
    assert get(srv, f"/api/view?job={job.id}", data={"viewer": "3d"})[1]["error"]["code"] == "BAD_ARGS"


def test_window_prefers_chrome_and_falls_back_to_the_default_browser(saos_home, tmp_path, monkeypatch):
    import saos.ui as ui_pkg

    monkeypatch.setattr(ui_pkg.shutil, "which", lambda name: None)
    for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        monkeypatch.delenv(key, raising=False)
    assert ui_pkg._chrome() is None
    # 按用户安装的 Chrome 在 LOCALAPPDATA 下
    exe = tmp_path / ui_pkg.CHROME_SUBPATH
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert ui_pkg._chrome() == str(exe)

    monkeypatch.setattr(ui_pkg.jobs, "get_job", lambda job_id: type("J", (), {"id": job_id})())
    monkeypatch.setattr(ui_pkg, "ensure_server", lambda: {"port": 1, "token": "x", "pid": 2, "reused": True})
    launched, browsed = [], []
    monkeypatch.setattr(ui_pkg.subprocess, "Popen", lambda cmd, **kw: launched.append(cmd))
    monkeypatch.setattr(ui_pkg.webbrowser, "open", lambda url: browsed.append(url) or True)
    assert ui_pkg.open_window("J1")["window"] == "chrome-app"
    assert launched[0][0] == str(exe) and launched[0][1].startswith("--app=") and not browsed
    monkeypatch.delenv("LOCALAPPDATA")
    assert ui_pkg.open_window("J1")["window"] == "browser" and len(launched) == 1 and browsed


def test_live_view_and_window_polls_are_served(ui):
    (daemon, job), srv = ui
    msg = get(srv, f"/api/live?job={job.id}")[1]
    assert msg["ok"] and msg["result"]["lens"] == "a.zmx" and msg["result"]["lens_data"] and "interference" in msg["result"]
    assert "spot" not in msg["result"]  # 当前视图只有布局和 Lens Data / the live view has layout and Lens Data only
    sec = msg["result"]["section"]  # 轴对称：2D 画全局 YZ 面 / axial: the 2D view is the global YZ plane
    assert (sec["u_name"], sec["v_name"], sec["fan"]) == ("z", "y", "px") and set(sec["profiles"].values()) == {"profile_y"}
    before = get(srv, "/api/ping")[1]["result"]["seen_s"]
    state = get(srv, f"/api/state?job={job.id}")[1]["result"]
    assert job.id not in before and get(srv, "/api/ping")[1]["result"]["seen_s"][job.id] < 5
    get(srv, f"/api/state?job={job.id}&hidden=1")
    assert get(srv, "/api/ping")[1]["result"]["hidden"] == [job.id]  # 最小化的窗口 / a minimised window
    first = get(srv, f"/api/state?job={job.id}")[1]["result"]["live"]
    assert get(srv, "/api/ping")[1]["result"]["hidden"] == []
    assert first == state["live"]
    call(daemon, "lens.set", job=job.id, surface=1, param="radius", value=60.0)
    assert get(srv, f"/api/state?job={job.id}")[1]["result"]["live"] != first


def test_job_commands_reopen_the_window_unless_one_is_polling(saos_home, monkeypatch):
    import argparse

    import saos.ui as ui_pkg
    from saos import cli

    job = jobs.create_job("watch")
    monkeypatch.delenv("SAOS_UI")
    monkeypatch.setattr(ui_pkg, "ensure_server", lambda: {"port": 1, "token": "x", "pid": 2, "reused": True})
    monkeypatch.setattr(ui_pkg, "_chrome", lambda: "chrome.exe")
    launched = []
    monkeypatch.setattr(ui_pkg.subprocess, "Popen", lambda cmd, **kw: launched.append(cmd))
    monkeypatch.setattr(ui_pkg, "_foreground", lambda: 0)
    monkeypatch.setattr(ui_pkg, "RECONNECT_WAIT_S", 0.0)
    assert ui_pkg.ensure_window(job.id)["window"] == "chrome-app" and len(launched) == 1
    assert "--disable-background-timer-throttling" in launched[0]  # 最小化后照常轮询 / keeps polling when minimised
    assert ui_pkg.ensure_window(job.id) is None and len(launched) == 1  # 刚打开，还没轮询 / just opened, not polled yet
    monkeypatch.setattr(ui_pkg.time, "time", lambda: 1e12)  # 很久以后，窗口没在轮询：再开 / much later, nobody polls: open again
    assert ui_pkg.ensure_window(job.id)["window"] == "chrome-app" and len(launched) == 2
    monkeypatch.setattr(server, "read_state", lambda: {"pid": 1, "port": 1, "token": "x"})
    monkeypatch.setattr(ui_pkg.client, "pid_alive", lambda pid: True)
    monkeypatch.setattr(ui_pkg, "_seen", lambda state: ({job.id: 1.5}, set()))
    monkeypatch.setattr(ui_pkg.time, "time", lambda: 2e12)
    assert ui_pkg.ensure_window(job.id) is None and len(launched) == 2  # 窗口开着 / the window is open
    # 最小化的窗口轮询变慢：40 s 前来过仍算开着；没隐藏的窗口 40 s 没来就是关了
    # a minimised window polls slowly: 40 s ago still counts as open; a visible one silent for 40 s is closed
    monkeypatch.setattr(ui_pkg, "_seen", lambda state: ({job.id: 40.0}, {job.id}))
    assert ui_pkg.ensure_window(job.id) is None and len(launched) == 2
    monkeypatch.setattr(ui_pkg, "_seen", lambda state: ({job.id: 40.0}, set()))
    monkeypatch.setattr(ui_pkg.time, "time", lambda: 3e12)
    assert ui_pkg.ensure_window(job.id)["window"] == "chrome-app" and len(launched) == 3
    monkeypatch.setenv("SAOS_UI", "0")
    monkeypatch.setattr(ui_pkg, "_seen", lambda state: ({}, set()))
    monkeypatch.setattr(ui_pkg.time, "time", lambda: 4e12)
    assert ui_pkg.ensure_window(job.id) is None and len(launched) == 3
    # 命令行只对改动或计算镜头的带任务命令这样做 / the CLI does it for job commands that change or compute the lens
    # 记一轮、建模说明、待确认的需求单是需要用户看的节点（gate），用户关掉的窗口在这里重开
    # a round, a model description and a demand awaiting confirmation are gates, where a window the user closed reopens
    asked = []
    monkeypatch.setattr(ui_pkg, "ensure_window", lambda job_id, gate=False: asked.append((job_id, gate)) or {"window": "chrome-app"})
    parser = cli.build_parser()
    assert cli._ensure_window(parser.parse_args(["round", "--job", job.id])) == {"window": "chrome-app"}
    assert cli._ensure_window(parser.parse_args(["scan", "--job", job.id, "--show"])) is None
    assert cli._ensure_window(parser.parse_args(["report", "--job", job.id])) is None
    cli._ensure_window(parser.parse_args(["opt", "local", "--job", job.id]))
    cli._ensure_window(parser.parse_args(["lens", "describe", "--job", job.id]))
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(DEMAND, allow_unicode=True), encoding="utf-8")
    cli._ensure_window(parser.parse_args(["demand", "show", "--job", job.id]))
    demand.confirm(job.root, "可以")
    assert cli._ensure_window(parser.parse_args(["demand", "show", "--job", job.id])) is None  # 已确认：只是重读 / just rereading
    assert asked == [(job.id, True), (job.id, False), (job.id, True), (job.id, True)]


def test_snapshot_shows_a_detached_scan_as_the_running_operation(saos_home):
    """分离运行的扫描写 scans/NNN/progress.json；后台进程活着时窗口把它当作正在运行的操作。"""
    import os

    job = jobs.create_job("detached")
    folder = job.root / "scans" / "001"
    folder.mkdir(parents=True)
    (folder / "progress.json").write_text(json.dumps({"op": "scan.run", "status": "running", "started": "2026-10-17T10:00:00+08:00",
                                                      "daemon_pid": os.getpid()}), encoding="utf-8")
    progress = stage.snapshot(job)["progress"]
    assert progress["op"] == "scan.run" and progress["detail"] == "scans/001"
    # 所属后台进程已经不在：不再显示 / the owning daemon is gone: not shown
    (folder / "progress.json").write_text(json.dumps({"op": "scan.run", "status": "running", "daemon_pid": 0}), encoding="utf-8")
    assert stage.snapshot(job)["progress"] is None


def _surface(index, vertex, y_dir, x_dir, sd=1.0):
    """平面镜片面：顶点、局部 y 轴、局部 x 轴的全局方向 / a flat face given its vertex and local y and x axes."""
    at = lambda d, k: [vertex[i] + d[i] * k for i in range(3)]  # noqa: E731
    return {"index": index, "vertex": list(vertex), "profile_y": [at(y_dir, k) for k in (-sd, 0.0, sd)],
            "profile_x": [at(x_dir, k) for k in (-sd, 0.0, sd)], "rim": [at(y_dir, sd), at(x_dir, sd), at(y_dir, -sd), at(x_dir, -sd)]}


def test_folded_systems_get_a_2d_section_in_their_plane_of_symmetry():
    """轴对称系统画 YZ 面；在 XZ 面内折转（光纤段沿 x，45° 镜后沿 z）画 XZ 面、用弧矢截线和 py = 0 的扇；各面不共面时没有 2D。"""
    X, Y, Z = (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)
    axial = {"surfaces": [_surface(1, (0, 0, 0), Y, X), _surface(2, (0, 0, 5), Y, X)],
             "layout": {"rays": [{"field": 0, "px": 0.0, "py": py, "points": [[0, py, 0], [0, py, 5]]} for py in (-1.0, 1.0)]}}
    sec = section.section_plane(axial)
    assert sec["normal"] == [1.0, 0.0, 0.0] and (sec["u_name"], sec["v_name"], sec["fan"]) == ("z", "y", "px")
    assert sec["profiles"] == {1: "profile_y", 2: "profile_y"}

    # 光纤段的面法线沿 x（局部 x 轴对着全局 -z），镜后的面法线沿 z；光瞳 x 扇（py = 0）留在 XZ 面里
    folded = {"surfaces": [_surface(1, (3, 0, 0), Y, (0, 0, -1)), _surface(2, (0, 0, 2), Y, X)],
              "layout": {"rays": [{"field": 0, "px": px, "py": 0.0, "points": [[3, 0, px], [px, 0, 2]]} for px in (-1.0, 1.0)]
                         + [{"field": 0, "px": 0.0, "py": py, "points": [[3, py, 0], [0, py, 2]]} for py in (-1.0, 1.0)]}}
    sec = section.section_plane(folded)
    assert sec["normal"] == [0.0, 1.0, 0.0] and (sec["u_name"], sec["v_name"], sec["fan"]) == ("z", "x", "py")
    assert sec["profiles"] == {1: "profile_x", 2: "profile_x"}

    skew = {"surfaces": [_surface(1, (0, 0, 0), Y, X), _surface(2, (1, 0, 1), Y, X), _surface(3, (0, 1, 2), Y, X),
                         _surface(4, (1, 1, 5), Y, X)], "layout": {"rays": []}}
    assert section.section_plane(skew) is None


def test_a_window_the_user_closed_stays_closed_until_a_gate(ui, monkeypatch):
    """关掉窗口时页面发 /api/closed；之后的普通命令不重开，记一轮等节点才重开；窗口再轮询就去掉标记。"""
    import saos.ui as ui_pkg

    (daemon, job), srv = ui
    monkeypatch.delenv("SAOS_UI")
    get(srv, f"/api/state?job={job.id}")
    assert get(srv, f"/api/closed?job={job.id}", data={})[1]["ok"]
    assert server.closed_jobs() == {job.id} and job.id not in get(srv, "/api/ping")[1]["result"]["seen_s"]
    opened = []
    monkeypatch.setattr(ui_pkg, "open_window", lambda job_id, **kw: opened.append(job_id) or {"window": "chrome-app"})
    assert ui_pkg.ensure_window(job.id) is None and opened == []
    assert ui_pkg.ensure_window(job.id, gate=True) == {"window": "chrome-app"} and opened == [job.id]
    get(srv, f"/api/state?job={job.id}")  # 重开的窗口来轮询 / the reopened window polls
    assert server.closed_jobs() == set()


def test_a_restarted_service_keeps_its_address_so_open_windows_reconnect(saos_home, monkeypatch):
    """服务重启沿用上次的端口和令牌；端口被占了才换新的。服务不在时先等窗口重连，不急着开第二个。"""
    import saos.ui as ui_pkg

    first = server._make_server()
    port, token = first.server_address[1], first.token
    first.server_close()
    second = server._make_server()
    assert (second.server_address[1], second.token) == (port, token)
    third = server._make_server()  # 端口还被第二个占着 / the port is still held by the second
    assert third.server_address[1] != port and json.loads(server.endpoint_file().read_text())["port"] == third.server_address[1]
    second.server_close()
    third.server_close()

    job = jobs.create_job("again")
    monkeypatch.delenv("SAOS_UI")
    ui_pkg.write_json(ui_pkg.config.daemon_dir() / ui_pkg.OPENED_FILE, {job.id: 0.0})
    monkeypatch.setattr(ui_pkg, "ensure_server", lambda: {"port": 1, "token": "x", "pid": 2, "reused": False})
    monkeypatch.setattr(ui_pkg, "_seen", lambda state: ({job.id: 0.3}, set()))
    monkeypatch.setattr(ui_pkg, "open_window", lambda job_id, **kw: pytest.fail("the old window reconnected; no second one"))
    assert ui_pkg.ensure_window(job.id) is None
