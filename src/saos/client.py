"""命令行一侧：启动、停止后台进程，并把请求转发给它。"""

from __future__ import annotations

import ctypes
import json
import os
import secrets
import subprocess
import sys
import time
from multiprocessing.connection import Client
from typing import Any

from saos import config
from saos.protocol import ErrorCode, SaosError, decode, encode

DEFAULT_REQUEST_TIMEOUT_S = 600.0
DEFAULT_START_TIMEOUT_S = 120.0
STOP_WAIT_S = 60.0

_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenProcess.restype = ctypes.c_void_p
_kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
_kernel32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
_kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)


def pid_alive(pid: int) -> bool:
    # 不能用 os.kill(pid, 0)：Windows 上 0 是 CTRL_C_EVENT
    if pid <= 0:
        return False
    handle = _kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5  # 拒绝访问说明进程存在
    try:
        code = ctypes.c_ulong()
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        _kernel32.CloseHandle(handle)


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


_kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
_kernel32.CreateToolhelp32Snapshot.argtypes = (ctypes.c_ulong, ctypes.c_ulong)
_kernel32.Process32FirstW.argtypes = (ctypes.c_void_p, ctypes.POINTER(_ProcessEntry))
_kernel32.Process32NextW.argtypes = (ctypes.c_void_p, ctypes.POINTER(_ProcessEntry))


def process_pids(image: str, parent: int | None = None) -> list[int]:
    """映像名为 image 的进程号；给出 parent 时只看它的直接子进程。"""
    snapshot = _kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return []
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(_ProcessEntry)
        found = []
        ok_ = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok_:
            if (parent is None or entry.th32ParentProcessID == parent) and entry.szExeFile.lower() == image.lower():
                found.append(int(entry.th32ProcessID))
            ok_ = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return found
    finally:
        _kernel32.CloseHandle(snapshot)


def child_pids(parent: int, image: str) -> list[int]:
    """parent 的直接子进程中，映像名为 image 的进程号。"""
    return process_pids(image, parent)


def read_state() -> dict[str, Any] | None:
    try:
        return json.loads(config.daemon_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _clear_stale_state() -> None:
    config.daemon_state_file().unlink(missing_ok=True)
    config.daemon_authkey_file().unlink(missing_ok=True)


def request(op: str, args: dict[str, Any] | None = None, timeout: float = DEFAULT_REQUEST_TIMEOUT_S) -> Any:
    """发送一个请求，成功时返回 result，失败时抛出 SaosError。"""
    state = read_state()
    if state is None or not pid_alive(int(state.get("pid", 0))):
        raise SaosError(ErrorCode.DAEMON_NOT_RUNNING, "the daemon is not running; run saos daemon start first")
    try:
        authkey = config.daemon_authkey_file().read_bytes()
        conn = Client(state["address"], family="AF_PIPE", authkey=authkey)
    except (OSError, KeyError) as exc:
        raise SaosError(ErrorCode.DAEMON_NOT_RUNNING, f"cannot connect to the daemon: {exc}") from exc
    try:
        conn.send_bytes(encode({"op": op, "args": args or {}}))
        if not conn.poll(timeout):
            raise SaosError(ErrorCode.DAEMON_TIMEOUT, f"no response within {timeout:.0f} s", {"op": op})
        response = decode(conn.recv_bytes())
    except (EOFError, OSError) as exc:
        raise SaosError(ErrorCode.DAEMON_NOT_RUNNING, f"connection to the daemon broke: {exc}") from exc
    finally:
        conn.close()
    if response.get("ok"):
        return response.get("result")
    err = response.get("error") or {}
    raise SaosError(err.get("code", ErrorCode.PROTOCOL_ERROR), err.get("message", "unknown error"), err.get("details"))


def _log_tail(lines: int = 20) -> list[str]:
    try:
        text = config.daemon_log_file().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-lines:]


def _kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)


def _spawn(mode: str, token: str) -> subprocess.Popen:
    config.daemon_dir().mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "saos", "daemon", "run", "--mode", mode, "--token", token]
    log_fh = config.daemon_log_file().open("ab")
    # DETACHED_PROCESS 只作用于 venv 启动器，其解释器子进程仍可能创建可见控制台。
    # CREATE_NO_WINDOW 让整条启动链继承无窗口控制台，不能与 DETACHED_PROCESS 混用。
    base_flags = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=log_fh, stderr=log_fh, cwd=str(config.saos_home()), close_fds=True, env={**os.environ, "SAOS_HOME": str(config.saos_home())})
    try:
        # 脱离调用方的作业对象，避免命令行退出时后台进程被一起结束
        return subprocess.Popen(cmd, creationflags=base_flags | _CREATE_BREAKAWAY_FROM_JOB, **kwargs)
    except OSError:
        return subprocess.Popen(cmd, creationflags=base_flags, **kwargs)
    finally:
        log_fh.close()


def status() -> dict[str, Any]:
    state = read_state()
    if state is None:
        return {"running": False}
    if not pid_alive(int(state.get("pid", 0))):
        _clear_stale_state()
        return {"running": False, "stale_state_removed": True}
    try:
        return {"running": True, **request("ping", timeout=10)}
    except SaosError as exc:
        return {"running": True, "responding": False, "pid": state.get("pid"), "error": exc.to_dict()}


def instance_daemons() -> list[dict[str, Any]]:
    """work/ 下每个实例的后台进程（只读状态文件，不连管道，忙着的进程也不会拖慢），以及它正在跑的任务。"""
    found = []
    if not config.WORK_ROOT.is_dir():
        return found
    current = config.saos_home()
    for home in sorted(p for p in config.WORK_ROOT.iterdir() if p.is_dir()):
        try:
            state = json.loads((home / "daemon" / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pid = int(state.get("pid", 0))
        item = {"instance": home.name, "current": home.resolve() == current, "pid": pid, "alive": pid_alive(pid),
                "mode": state.get("mode"), "started_at": state.get("started_at"), "running_jobs": []}
        for progress_file in sorted((home / "jobs").glob("*/progress.json")):
            try:
                progress = json.loads(progress_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if progress.get("status") == "running" and progress.get("daemon_pid") == pid:
                item["running_jobs"].append({k: progress.get(k) for k in ("op", "detail", "started", "stage")}
                                            | {"job": progress_file.parent.name})
        found.append(item)
    return found


def environment() -> dict[str, Any]:
    """会话开始时一次查清外部条件：各实例的后台进程、OpticStudio 进程占用的许可、可见主窗口、extension 能否启动。"""
    from saos import native_ui

    opticstudio = process_pids("OpticStudio.exe")
    windows = native_ui.main_windows()
    return {
        "opticstudio_processes": opticstudio,
        "licence": {"limit": config.LICENSED_INSTANCES, "in_use": len(opticstudio),
                    "free": max(0, config.LICENSED_INSTANCES - len(opticstudio))},
        "windows": [{"pid": w["pid"], "title": w["title"]} for w in windows],
        "extension": native_ui.extension_readiness(windows),
        "instances": instance_daemons(),
    }


def start(mode: str, timeout: float = DEFAULT_START_TIMEOUT_S) -> dict[str, Any]:
    current = status()
    if current.get("running"):
        raise SaosError(ErrorCode.DAEMON_ALREADY_RUNNING, "the daemon is already running", current)

    error_file = config.daemon_dir() / "start_error.json"
    error_file.unlink(missing_ok=True)
    started = time.monotonic()
    # venv 的 python.exe 是启动器，真正的解释器是它的子进程，PID 对不上，所以用一次性令牌识别
    token = secrets.token_hex(8)
    proc = _spawn(mode, token)
    while time.monotonic() - started < timeout:
        if proc.poll() is not None:
            if error_file.is_file():
                err = json.loads(error_file.read_text(encoding="utf-8"))
                raise SaosError(err["code"], err["message"], {**(err.get("details") or {}), "log_tail": _log_tail()})
            raise SaosError(ErrorCode.DAEMON_START_FAILED, f"the daemon exited with code {proc.returncode}", {"log_tail": _log_tail()})
        state = read_state()
        if state is not None and state.get("token") == token:
            info = request("ping", timeout=10)
            return {"started": True, "startup_s": round(time.monotonic() - started, 2), **info}
        time.sleep(0.25)
    _kill_tree(proc.pid)
    _clear_stale_state()
    raise SaosError(ErrorCode.DAEMON_TIMEOUT, f"the daemon was not ready within {timeout:.0f} s and was killed", {"log_tail": _log_tail()})


def stop(discard: bool = False, force: bool = False) -> dict[str, Any]:
    state = read_state()
    if state is None or not pid_alive(int(state.get("pid", 0))):
        _clear_stale_state()
        raise SaosError(ErrorCode.DAEMON_NOT_RUNNING, "the daemon is not running")
    pid = int(state["pid"])
    # OpticStudio 在后台进程退出后才异步关闭，要等它也退出，避免紧接着重启时两个实例同时占用许可证
    opticstudio = child_pids(pid, "OpticStudio.exe")
    try:
        request("shutdown", {"discard": discard}, timeout=30)
    except SaosError as exc:
        if not force:
            raise
        _kill_tree(pid)
        _clear_stale_state()
        return {"stopped": True, "pid": pid, "forced": True, "reason": exc.to_dict()}

    deadline = time.monotonic() + STOP_WAIT_S
    while time.monotonic() < deadline:
        if not pid_alive(pid) and not any(pid_alive(p) for p in opticstudio):
            return {"stopped": True, "pid": pid, "forced": False, "opticstudio_pids": opticstudio}
        time.sleep(0.25)
    if force:
        for p in [pid, *opticstudio]:
            if pid_alive(p):
                _kill_tree(p)
        _clear_stale_state()
        return {"stopped": True, "pid": pid, "forced": True, "reason": "exit timed out", "opticstudio_pids": opticstudio}
    raise SaosError(
        ErrorCode.DAEMON_TIMEOUT,
        f"the daemon or its OpticStudio did not exit within {STOP_WAIT_S:.0f} s; use --force to kill it",
        {"pid": pid, "opticstudio_pids": [p for p in opticstudio if pid_alive(p)]},
    )
