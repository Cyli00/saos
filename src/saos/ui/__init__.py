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
# 最小化的窗口：浏览器把定时器放慢到最多约 1 分钟一次（Chrome 的 intensive throttling），留出余量
# a minimised window: browsers slow its timers to about once a minute (Chrome's intensive throttling); with a margin
HIDDEN_SEEN_WITHIN_S = 90.0
# UI 专用的 Chrome 配置不放慢后台窗口 / the UI's own Chrome profile keeps background windows at full speed
CHROME_FLAGS = ("--disable-background-timer-throttling", "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows")
# 守焦点的后台进程最多等这么久：Chrome 冷启动后 4.5 s 才弹出窗口抢焦点（2026-10-19 实测）
# how long the background focus guard waits: a cold Chrome took the focus 4.5 s after the start (measured 2026-10-19)
FOREGROUND_WAIT_S = 15.0
# 刚打开的窗口还没来得及轮询，这段时间内不再开第二个 / a window just opened has not polled yet; do not open a second one meanwhile
OPENING_S = 20.0
# 服务重启后，开着的窗口在原地址重连（端口和令牌沿用）；等这么久再决定要不要开新窗口
# after a restart open windows reconnect at the same address; wait this long before opening another one
RECONNECT_WAIT_S = 4.0
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


def _seen(state: dict[str, Any]) -> tuple[dict[str, float], set[str]] | None:
    """(各任务窗口距上次轮询的秒数, 上次轮询时隐藏着的任务)；服务不应答时 None。"""
    req = urllib.request.Request(f"http://127.0.0.1:{state['port']}/api/ping", headers={"X-Saos-Token": state["token"]})
    try:
        with _DIRECT.open(req, timeout=2) as resp:
            result = json.loads(resp.read())["result"]
            return result.get("seen_s") or {}, set(result.get("hidden") or ())
    except (OSError, ValueError, KeyError):
        return None


def ensure_window(job_id: str, *, gate: bool = False) -> dict[str, Any] | None:
    """带任务的命令执行前调用：这个任务的流程图窗口最近没有轮询过（没打开、服务已退出）就打开它，不靠 agent 记得。
    用户自己关掉的窗口不再打开，直到需要用户看的节点（gate=True：需求待确认、建模说明、记一轮）。
    SAOS_UI=0 时什么都不做。返回打开的结果，没有打开时返回 None。
    Called before a job command: when no window of this job has polled lately (never opened, service gone), open one,
    so it does not depend on the agent remembering. A window the user closed stays closed until a point where the user
    has to look (gate=True: a demand awaiting confirmation, a model description, a round). Does nothing with SAOS_UI=0."""
    from saos.ui import server

    if os.environ.get("SAOS_UI") == "0":
        return None
    if not gate and job_id in server.closed_jobs():
        return None
    state = server.read_state()
    polled = _seen(state) if state and client.pid_alive(int(state.get("pid", 0))) else None
    if polled is not None:
        seen, hidden = polled
        if seen.get(job_id, math.inf) <= (HIDDEN_SEEN_WITHIN_S if job_id in hidden else SEEN_WITHIN_S):
            # 开着的窗口在需要用户看的时候恢复（最小化时）并放到最上层 / at a gate an open window is restored and raised
            return {"window": "raised" if _raise_window(job_id) else "open"} if gate else None
    elif job_id in _opened() and job_id not in server.closed_jobs():
        # 服务不在了，但这个任务的窗口可能还开着：先起服务，给窗口一个重连的机会 / the window may still be open: let it reconnect
        state = ensure_server()
        deadline = time.monotonic() + RECONNECT_WAIT_S
        while time.monotonic() < deadline:
            again = _seen(state)
            if again is not None and job_id in again[0]:
                return None
            time.sleep(0.3)
    # 刚打开的窗口还没来轮询时不再开第二个；用户已经关掉的窗口肯定不在了，不受这条限制
    # no second window while one just opened has not polled yet; a window the user closed is certainly gone
    if job_id not in server.closed_jobs() and time.time() - float(_opened().get(job_id, 0)) < OPENING_S:
        return None
    # 自动重开不抢焦点：用户可能正在别的窗口里打字 / an automatic reopen keeps the focus where the user is typing
    return open_window(job_id, activate=False)


def _raise_window(job_id: str) -> bool:
    """Windows：按标题（saos · <任务号>）找到这个任务的窗口，最小化的先恢复，再放到最上层，都不夺键盘焦点
    （用户可能正在别的窗口里打字）。找不到窗口时 False。
    Windows: find the job's window by its title, restore it if minimised and put it on top, both without taking the
    keyboard focus. False when no window is found."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    title = f"saos · {job_id}"
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        length = user32.GetWindowTextLengthW(hwnd)
        if length and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            # 整个标题相同，或其后是空格（浏览器追加的后缀）；任务号 abc 不会匹配到 abc-2 的窗口
            # the whole title, or it followed by a space (a browser's suffix); job abc never matches the window of abc-2
            if buf.value == title or buf.value.startswith(title + " "):
                found.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    # SW_SHOWNOACTIVATE；先置为 TOPMOST 再取消，窗口留在最上层而不激活 / topmost then not: on top, not activated
    flags = 0x0001 | 0x0002 | 0x0010  # SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
    for hwnd in found:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 4)
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, flags)
        user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, flags)
    return bool(found)


def notify(job_id: str, message: str) -> dict[str, Any]:
    """agent 停下来问用户之前调用：在窗口顶部留一句话，并像需要用户看的节点那样把窗口打开或置前。
    Called before the agent stops to ask the user: leave a line at the top of the window and open or raise it like a gate."""
    job = jobs.get_job(job_id)
    if not message.strip():
        raise SaosError(ErrorCode.BAD_ARGS, "--message must say what the user should look at or answer")
    state = ensure_server()
    body = json.dumps({"job": job.id, "message": message}).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{state['port']}/api/notice?job={job.id}", data=body, method="POST",
                                 headers={"X-Saos-Token": state["token"], "Content-Type": "application/json"})
    with _DIRECT.open(req, timeout=5) as resp:
        notice = json.loads(resp.read())["result"]["notice"]
    window = None if os.environ.get("SAOS_UI") == "0" else ensure_window(job.id, gate=True)
    return {"job": job.id, "notice": notice, "window": window}


def _opened() -> dict[str, float]:
    try:
        return json.loads((config.daemon_dir() / OPENED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _foreground() -> int:
    if sys.platform != "win32":
        return 0
    import ctypes

    return int(ctypes.windll.user32.GetForegroundWindow() or 0)


def _keep_focus(before: int, wait: float = FOREGROUND_WAIT_S) -> bool:
    """Chrome 不理会 STARTUPINFO 的“显示但不激活”（2026-10-17 实测三种启动方式都抢焦点），所以等新窗口抢到前台后，
    挂到前台线程的输入队列上把焦点还给原来的窗口。返回是否还回去了。
    Chrome ignores STARTUPINFO's show-without-activating (all three start modes took the focus, measured 2026-10-17), so
    once the new window is in front, attach to the foreground thread's input and give the focus back."""
    if sys.platform != "win32" or not before:
        return False
    import ctypes

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        now = user32.GetForegroundWindow()
        if now and now != before:
            thread = user32.GetWindowThreadProcessId(now, None)
            me = kernel32.GetCurrentThreadId()
            user32.AttachThreadInput(me, thread, True)
            try:
                user32.SetForegroundWindow(before)
            finally:
                user32.AttachThreadInput(me, thread, False)
            if int(user32.GetForegroundWindow() or 0) != before:
                # 后台进程（agent 调用命令时就是）不许设前台窗口，上面这招失败：把新窗口最小化，激活自然回到原来的窗口，
                # 再“显示但不激活”恢复它（2026-10-19 实测有效）
                # a background process may not set the foreground window: minimise the new window so activation returns
                # to the previous one, then restore it without activating (measured 2026-10-19)
                user32.ShowWindow(now, 6)  # SW_MINIMIZE：激活 Z 序里的下一个窗口 / activates the next window in Z order
                time.sleep(0.3)
                user32.ShowWindow(now, 4)  # SW_SHOWNOACTIVATE
            return int(user32.GetForegroundWindow() or 0) == before
        time.sleep(0.05)
    return False


def _guard_focus(before: int) -> None:
    """在脱离的后台进程里守焦点，命令不必等 Chrome 启动完 / guard the focus in a detached process; the command does not wait"""
    flags = client._CREATE_NO_WINDOW | client._CREATE_NEW_PROCESS_GROUP
    cmd = [sys.executable, "-c", f"from saos.ui import _keep_focus; _keep_focus({int(before)})"]
    try:
        subprocess.Popen(cmd, creationflags=flags | client._CREATE_BREAKAWAY_FROM_JOB, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    except OSError:
        subprocess.Popen(cmd, creationflags=flags, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


def open_window(job_id: str, *, window: bool = True, activate: bool = True) -> dict[str, Any]:
    job = jobs.get_job(job_id)
    state = ensure_server()
    url = f"http://127.0.0.1:{state['port']}/?job={job.id}&t={state['token']}"
    opened, guarded = None, False
    if window:
        chrome = _chrome()
        if chrome:
            # 单独的配置目录让窗口大小生效，也不打扰用户自己的 Chrome / a separate profile makes the size stick and leaves the user's Chrome alone
            profile = config.daemon_dir() / "ui-chrome"
            before = 0 if activate else _foreground()
            subprocess.Popen([chrome, f"--app={url}", f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}", f"--user-data-dir={profile}",
                              "--no-first-run", "--no-default-browser-check", *CHROME_FLAGS], close_fds=True)
            opened = "chrome-app"
            if before:
                _guard_focus(before)
                guarded = True
        elif webbrowser.open(url):
            opened = "browser"
        if opened:
            write_json(config.daemon_dir() / OPENED_FILE, {**_opened(), job.id: time.time()})
    out = {"job": job.id, "url": url, "pid": state["pid"], "server_reused": state["reused"], "window": opened}
    if guarded:
        out["focus_guard"] = True
    return out
