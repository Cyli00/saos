"""saos ui：流程图小窗口。服务没在跑就以后台进程启动，再用 Edge 的应用窗口（无地址栏）打开。
saos ui: the small workflow window. Starts the service in the background when needed, then opens it in an Edge app window.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from saos import client, config, jobs
from saos.protocol import ErrorCode, SaosError

START_TIMEOUT_S = 15.0
WINDOW_SIZE = (1080, 720)
EDGE_PATHS = (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")


# 不经 HTTP_PROXY 之类的代理设置直连本机 / talk to 127.0.0.1 directly, ignoring HTTP_PROXY and similar settings
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _responding(state: dict[str, Any]) -> bool:
    req = urllib.request.Request(f"http://127.0.0.1:{state['port']}/api/ping", headers={"X-Saos-Token": state["token"]})
    try:
        with _DIRECT.open(req, timeout=2) as resp:
            return json.loads(resp.read()).get("ok") is True
    except (OSError, ValueError):
        return False


def ensure_server() -> dict[str, Any]:
    from saos.ui import server

    state = server.read_state()
    if state and client.pid_alive(int(state.get("pid", 0))) and _responding(state):
        return {**state, "reused": True}
    server.state_file().unlink(missing_ok=True)
    config.daemon_dir().mkdir(parents=True, exist_ok=True)
    log_fh = (config.daemon_dir() / "ui.log").open("ab")
    try:
        # 与后台进程同样的无窗口、脱离作业对象的启动方式（saos.client._spawn）/ same detached, windowless start as the daemon
        flags = client._CREATE_NO_WINDOW | client._CREATE_NEW_PROCESS_GROUP
        kwargs = dict(stdin=subprocess.DEVNULL, stdout=log_fh, stderr=log_fh, cwd=str(config.saos_home()), close_fds=True,
                      env={**os.environ, "SAOS_HOME": str(config.saos_home())})
        cmd = [sys.executable, "-m", "saos", "ui", "serve"]
        try:
            proc = subprocess.Popen(cmd, creationflags=flags | client._CREATE_BREAKAWAY_FROM_JOB, **kwargs)
        except OSError:
            proc = subprocess.Popen(cmd, creationflags=flags, **kwargs)
    finally:
        log_fh.close()
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SaosError(ErrorCode.INTERNAL_ERROR, f"saos ui exited with code {proc.returncode}; see {config.daemon_dir() / 'ui.log'}")
        state = server.read_state()
        if state and _responding(state):
            return {**state, "reused": False}
        time.sleep(0.2)
    raise SaosError(ErrorCode.DAEMON_TIMEOUT, f"saos ui did not start within {START_TIMEOUT_S:.0f} s")


def _edge() -> str | None:
    found = shutil.which("msedge")
    return found or next((p for p in EDGE_PATHS if Path(p).is_file()), None)


def open_window(job_id: str, *, window: bool = True) -> dict[str, Any]:
    job = jobs.get_job(job_id)
    state = ensure_server()
    url = f"http://127.0.0.1:{state['port']}/?job={job.id}&t={state['token']}"
    opened = None
    if window:
        edge = _edge()
        if edge:
            # 单独的配置目录让窗口大小生效，也不打扰用户自己的 Edge / a separate profile makes the size stick and leaves the user's Edge alone
            profile = config.daemon_dir() / "ui-edge"
            subprocess.Popen([edge, f"--app={url}", f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}", f"--user-data-dir={profile}",
                              "--no-first-run", "--no-default-browser-check"], close_fds=True)
            opened = "edge-app"
        elif webbrowser.open(url):
            opened = "browser"
    return {"job": job.id, "url": url, "pid": state["pid"], "server_reused": state["reused"], "window": opened}
