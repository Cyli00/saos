"""saos ui：流程图小窗口。服务没在跑就以后台进程启动，再用 Chrome 的应用窗口（无地址栏）打开；没装 Chrome 时交给系统默认浏览器。
saos ui: the small workflow window. Starts the service in the background when needed, then opens it in a Chrome app window,
or in the system's default browser when Chrome is not installed.
"""

from __future__ import annotations

import json
import math
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
from saos.protocol import ErrorCode, SaosError, write_json

START_TIMEOUT_S = 15.0
# 窗口每 1.5 s 轮询一次；这么久没来过就当它没开着 / the window polls every 1.5 s; silent this long means it is not open
SEEN_WITHIN_S = 6.0
# 刚打开的窗口还没来得及轮询，这段时间内不再开第二个 / a window just opened has not polled yet; do not open a second one meanwhile
OPENING_S = 20.0
OPENED_FILE = "ui-opened.json"
WINDOW_SIZE = (1080, 720)
CHROME_SUBPATH = Path("Google") / "Chrome" / "Application" / "chrome.exe"


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


def _chrome() -> str | None:
    found = shutil.which("chrome")
    if found:
        return found
    # 系统级安装在 Program Files，按用户安装在 LOCALAPPDATA / machine-wide installs live in Program Files, per-user ones in LOCALAPPDATA
    roots = [os.environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    return next((str(Path(r) / CHROME_SUBPATH) for r in roots if r and (Path(r) / CHROME_SUBPATH).is_file()), None)


def _seen(state: dict[str, Any]) -> dict[str, float] | None:
    req = urllib.request.Request(f"http://127.0.0.1:{state['port']}/api/ping", headers={"X-Saos-Token": state["token"]})
    try:
        with _DIRECT.open(req, timeout=2) as resp:
            return json.loads(resp.read())["result"].get("seen_s") or {}
    except (OSError, ValueError, KeyError):
        return None


def ensure_window(job_id: str) -> dict[str, Any] | None:
    """带任务的命令执行前调用：这个任务的流程图窗口最近没有轮询过（没打开、被关掉、服务已退出）就打开它，
    不靠 agent 记得。SAOS_UI=0 时什么都不做。返回打开的结果，窗口本来就开着时返回 None。
    Called before a job command: when no window of this job has polled lately (never opened, closed, service gone),
    open one, so it does not depend on the agent remembering. Does nothing with SAOS_UI=0. None when it is already open."""
    from saos.ui import server

    if os.environ.get("SAOS_UI") == "0":
        return None
    state = server.read_state()
    if state and client.pid_alive(int(state.get("pid", 0))):
        seen = _seen(state)
        if seen is not None and seen.get(job_id, math.inf) <= SEEN_WITHIN_S:
            return None
    if time.time() - float(_opened().get(job_id, 0)) < OPENING_S:
        return None
    return open_window(job_id)


def _opened() -> dict[str, float]:
    try:
        return json.loads((config.daemon_dir() / OPENED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def open_window(job_id: str, *, window: bool = True) -> dict[str, Any]:
    job = jobs.get_job(job_id)
    state = ensure_server()
    url = f"http://127.0.0.1:{state['port']}/?job={job.id}&t={state['token']}"
    opened = None
    if window:
        chrome = _chrome()
        if chrome:
            # 单独的配置目录让窗口大小生效，也不打扰用户自己的 Chrome / a separate profile makes the size stick and leaves the user's Chrome alone
            profile = config.daemon_dir() / "ui-chrome"
            subprocess.Popen([chrome, f"--app={url}", f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}", f"--user-data-dir={profile}",
                              "--no-first-run", "--no-default-browser-check"], close_fds=True)
            opened = "chrome-app"
        elif webbrowser.open(url):
            opened = "browser"
        if opened:
            write_json(config.daemon_dir() / OPENED_FILE, {**_opened(), job.id: time.time()})
    return {"job": job.id, "url": url, "pid": state["pid"], "server_reused": state["reused"], "window": opened}
