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
# 分离运行的子进程等多久（一周）：计算本身有 --max-seconds 和 saos job cancel 管着 / how long a detached child waits (a week)
DETACHED_TIMEOUT_S = 7 * 24 * 3600.0
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
    # PID 是 32 位 DWORD；更大的数会被 ctypes 截成低 32 位，误判成别的进程（如 2**32 + 4 → System）
    if not 0 < pid < 2**32:
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


def running_ops(pid: int, home=None) -> list[dict[str, Any]]:
    """后台进程 pid 正在跑的长操作（各任务 progress.json 里 status 为 running、daemon_pid 相同的），只读文件。
    The long operations daemon pid is running (status running with its daemon_pid in a job's progress.json); files only."""
    found = []
    for progress_file in sorted(((home or config.saos_home()) / "jobs").glob("*/progress.json")):
        try:
            progress = json.loads(progress_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if progress.get("status") == "running" and progress.get("daemon_pid") == pid:
            found.append({k: progress.get(k) for k in ("op", "detail", "started", "stage", "updated")} | {"job": progress_file.parent.name})
    return found


# 后台进程忙着长操作时照样排队的请求：ping 有自己的短超时，shutdown 由后台进程自己判断能否退出
# requests that still queue behind a long operation: ping has its own short timeout, shutdown is judged by the daemon
QUEUE_WHEN_BUSY = frozenset({"ping", "shutdown"})


def request(op: str, args: dict[str, Any] | None = None, timeout: float = DEFAULT_REQUEST_TIMEOUT_S, *, queue: bool = False) -> Any:
    """发送一个请求，成功时返回 result，失败时抛出 SaosError。后台进程一次只处理一个请求：它正跑着长操作时直接报 BUSY，
    不排在后面等到客户端超时，免得调用方把超时当失败、重复提交昂贵的计算。
    Send a request; return its result or raise SaosError. The daemon serves one request at a time: while it runs a long
    operation the request fails at once with BUSY instead of queueing until the client times out, so that a caller does
    not take the timeout for a failure and submit an expensive computation again. queue=True waits its turn instead
    (the workflow window's control panel, which tells the user it is queued)."""
    state = read_state()
    if state is None or not pid_alive(int(state.get("pid", 0))):
        raise SaosError(ErrorCode.DAEMON_NOT_RUNNING, "the daemon is not running; run saos daemon start first")
    if op not in QUEUE_WHEN_BUSY and not queue:
        busy = running_ops(int(state["pid"]))
        if busy:
            raise SaosError(ErrorCode.BUSY, f"the daemon is still running {busy[0]['op']} for job {busy[0]['job']}; it keeps computing. "
                                            "Follow it with saos job show <job> (or saos job cancel <job>) and send this request when it is done",
                            {"running": busy, "op": op})
    try:
        authkey = config.daemon_authkey_file().read_bytes()
        conn = Client(state["address"], family="AF_PIPE", authkey=authkey)
    except (OSError, KeyError) as exc:
        raise SaosError(ErrorCode.DAEMON_NOT_RUNNING, f"cannot connect to the daemon: {exc}") from exc
    try:
        conn.send_bytes(encode({"op": op, "args": args or {}}))
        if not conn.poll(timeout):
            job = (args or {}).get("job")
            raise SaosError(ErrorCode.DAEMON_TIMEOUT,
                            f"no response within {timeout:.0f} s. This only stops waiting: the daemon keeps computing {op} until it "
                            "finishes or is cancelled, so do not send it again. Follow it with saos job show"
                            + (f" {job}" if job else " <job>") + "; next time pass --detach to get the result in the job's runs/ instead",
                            {"op": op, "still_running": True, "running": running_ops(int(state["pid"]))})
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
                "mode": state.get("mode"), "started_at": state.get("started_at"),
                "running_jobs": [{k: r[k] for k in ("op", "detail", "started", "stage", "job")} for r in running_ops(pid, home)]}
        found.append(item)
    return found


# ---- 分离运行：命令立即返回，结果写进任务的 runs/ / detached runs: the command returns at once, the result goes to the job's runs/ ----

RUNS_DIR = "runs"


def detach(op: str, args: dict[str, Any], job_root) -> dict[str, Any]:
    """在一个脱离的子进程里发送请求并等到结束，结果写成 runs/NNN-<op>.json；本命令立即返回。调用方的等待时限
    （代理工具常见 2–4 分钟）因此不再决定一次计算的成败。
    Send the request from a detached child process that waits for the end and writes runs/NNN-<op>.json; this command
    returns at once, so the caller's own wait limit (often 2–4 minutes for agent tools) no longer decides a computation."""
    from saos import jobs
    from saos.protocol import write_json

    state = read_state()
    if state is None or not pid_alive(int(state.get("pid", 0))):
        raise SaosError(ErrorCode.DAEMON_NOT_RUNNING, "the daemon is not running; run saos daemon start first")
    busy = running_ops(int(state["pid"]))
    if busy:
        raise SaosError(ErrorCode.BUSY, f"the daemon is still running {busy[0]['op']} for job {busy[0]['job']}; follow it with saos job show",
                        {"running": busy, "op": op})
    folder = job_root / RUNS_DIR
    folder.mkdir(exist_ok=True)
    stem = f"{jobs.next_archive_number(folder):03d}-{op.replace('.', '-')}"
    request_file = folder / f"{stem}.request.json"
    write_json(request_file, {"op": op, "args": args, "created": jobs.now_iso()})
    cmd = [sys.executable, "-c", "import sys; from saos.client import run_detached; sys.exit(run_detached(sys.argv[1]))", str(request_file)]
    base_flags = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(config.saos_home()),
                  close_fds=True, env={**os.environ, "SAOS_HOME": str(config.saos_home())})
    try:
        proc = subprocess.Popen(cmd, creationflags=base_flags | _CREATE_BREAKAWAY_FROM_JOB, **kwargs)
    except OSError:
        proc = subprocess.Popen(cmd, creationflags=base_flags, **kwargs)
    rel = f"{RUNS_DIR}/{stem}.json"
    # venv 的 python.exe 是启动器，真正发请求的解释器是它的子进程，PID 写在结果文件的 pid 里
    # the venv python.exe is a launcher; the interpreter that sends the request is its child, whose PID the run file records
    return {"detached": True, "op": op, "run": rel, "launcher_pid": proc.pid,
            "follow": f"saos job show {job_root.name} (progress and this run's status); the result lands in {rel}"}


def run_detached(request_path: str) -> int:
    """分离子进程的入口：发送 request.json 里的请求，等到结束（不设客户端时限），把响应写到同名的 .json。
    Entry of the detached child: send the request in request.json, wait for its end without a client limit, and write
    the response next to it as .json."""
    from pathlib import Path

    from saos import jobs
    from saos.protocol import jsonable, write_json

    path = Path(request_path)
    out = path.with_name(path.name.replace(".request.json", ".json"))
    spec = json.loads(path.read_text(encoding="utf-8"))
    record: dict[str, Any] = {"op": spec["op"], "status": "running", "pid": os.getpid(), "started": jobs.now_iso()}
    write_json(out, record)
    try:
        record.update(status="done", ok=True, result=jsonable(request(spec["op"], spec.get("args") or {}, timeout=DETACHED_TIMEOUT_S)))
    except SaosError as exc:
        record.update(status="failed", ok=False, error=exc.to_dict())
    except Exception as exc:  # noqa: BLE001 - 结果文件必须写出来 / the result file must be written
        record.update(status="failed", ok=False, error={"code": ErrorCode.INTERNAL_ERROR, "message": f"{type(exc).__name__}: {exc}"})
    record["finished"] = jobs.now_iso()
    write_json(out, record)
    return 0 if record["ok"] else 1


def detached_runs(job_root, last: int = 5) -> list[dict[str, Any]]:
    """任务最近几次分离运行的状态；子进程已不在而结果没写出来的记为 lost。/ Recent detached runs; a vanished child without a result is lost."""
    folder = job_root / RUNS_DIR
    if not folder.is_dir():
        return []
    out = []
    for request_file in sorted(folder.glob("*.request.json"))[-last:]:
        result_file = request_file.with_name(request_file.name.replace(".request.json", ".json"))
        try:
            record = json.loads(result_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = {"status": "starting"}
        if record.get("status") == "running" and not pid_alive(int(record.get("pid") or 0)):
            record["status"] = "lost"
        row = {"run": f"{RUNS_DIR}/{result_file.name}", "status": record.get("status")}
        row.update({k: record[k] for k in ("op", "started", "finished", "ok") if k in record})
        if record.get("error"):
            row["error"] = {k: record["error"].get(k) for k in ("code", "message")}
        out.append(row)
    return out


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
