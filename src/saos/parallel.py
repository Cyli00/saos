"""并发评估：几个 standalone OpticStudio 实例同时调用 ZOS-API（ADR-0008）。
Concurrent evaluation: several standalone OpticStudio instances call ZOS-API at the same time (ADR-0008).

在 saos exec 脚本里 / in a saos exec script:

    def run(ctx):
        points = search.grid({"lens1 back.curvature": [0, 1, 2], "3.conic": [-1, 0]})
        return ctx.map("point", points, workers=6)

    def point(w, p):              # 同一个脚本里的函数，在 worker 中运行 / a function of the same script, run in a worker
        w.set(p)
        return w.measure(["spot", "working_distance"])

fn 可以是本脚本里的函数名、任务内文件 "explore/x.py:fn"，或 saos 自带的 "saos.scan:point"。w 有 kit 的全部方法
（set、get、spot、trace、op、analysis……）、measure（需求单判定，同一套定义）、args、path、reload、worker。
fn is a function name of this script, a job file "explore/x.py:fn", or a built-in "saos.scan:point". w has every kit
method (set, get, spot, trace, op, analysis, …), measure (the demand's checks, same definitions), args, path, reload, worker.

worker 池常驻在后台进程里：第一次调用时启动（每个实例 5–8 s），之后的调用直接复用，空闲 SAOS_POOL_IDLE_S 秒
（默认 600）后关闭并释放许可证；ctx.pool_close() 立即关闭。每次调用把已保存的当前镜头复制给每个 worker，
默认每个条目开始前重新加载，条目之间互不影响。许可证拒绝的 worker 记为不可用，其余照常工作。
The worker pool lives in the daemon: it starts on first use (5–8 s per instance) and later calls reuse it; after
SAOS_POOL_IDLE_S seconds idle (default 600) it closes and frees the licences; ctx.pool_close() closes it now. Every call
gives each worker a copy of the saved current lens, reloaded before every item by default. A worker refused by the
licence is reported as unavailable; the others go on.

实测（2026-10-15，本机，Cooke 样例逐次改半径取 RSCE）：1、2、4 个实例分别 88.7、161、304 次/秒；
许可证上限为同时 8 个实例（含手动打开的窗口）：已开 3 个时，再开 6 或 8 个都只有 5 个拿到 ZOS-API 许可。
Measured (2026-10-15, this machine): 1, 2 and 4 instances gave 88.7, 161 and 304 evaluations/s; the licence allows
8 instances at a time, open windows included: with 3 open, only 5 of 6 or 8 new ones got ZOS-API.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from saos import config, jobs, stages
from saos.kit import KitMethods
from saos.protocol import ErrorCode, SaosError, jsonable, write_json

PARALLEL_DIR = "parallel"
DEFAULT_WORKERS = 4


def default_workers(backend: str | None = None) -> int:
    """默认 worker 数：Optiland worker 不占许可证，用满可用核（逻辑核数减 2，Plan 2）；OpticStudio worker 受许可证限制，仍为 4。
    Default worker count: Optiland workers take no licence and use the available cores (logical cores minus 2); OpticStudio 4."""
    return config.compute_cores() if (backend or config.backend_name()) == "optiland" else DEFAULT_WORKERS


def backend_for(job: jobs.Job) -> str:
    """认可之后的任务用 OpticStudio worker，其余用 Optiland；不按文件后缀或伴随文件判断。"""
    return "zos" if stages.backend(job.root) == "zos" else config.backend_name()


# worker 的协议行带前缀，和 OpticStudio 自己打到标准输出的文字区分开（实测会打印 "*** FRU__delta_init() …"）
# protocol lines carry a prefix, apart from what OpticStudio itself prints to stdout (e.g. "*** FRU__delta_init() …")
PREFIX = "@@saos "
BELOW_NORMAL = 0x00004000  # Windows BELOW_NORMAL_PRIORITY_CLASS


def idle_seconds() -> float:
    return float(os.environ.get("SAOS_POOL_IDLE_S", "600"))


class WorkerContext(KitMethods):
    """worker 里 fn(w, item) 能用的入口。/ What fn(w, item) can use inside a worker."""

    def __init__(self, backend, job_root: Path, base: Path | None, index: int, args: dict[str, Any] | None = None,
                 cache: dict[Any, Any] | None = None):
        self.backend = backend
        self.job_root = Path(job_root)
        self.job = jobs.Job(self.job_root.name, self.job_root)
        self.base = base
        self.worker = index
        self.args = args or {}
        self.cache: dict[Any, Any] = {} if cache is None else cache

    @property
    def system(self):
        return self.backend.system

    @property
    def zosapi(self):
        from saos.backend.zos import _load_zosapi

        return _load_zosapi(self.backend.root)

    def reload(self) -> None:
        """重新加载这个 worker 的镜头副本，并清空 cache。/ Reload this worker's copy of the lens and clear the cache."""
        self.backend.load(self.base)
        self.cache.clear()

    def path(self, rel: str) -> Path:
        return jobs.inside(self.job_root, rel)

    def child(self, args: dict[str, Any], cache: dict[Any, Any] | None = None) -> WorkerContext:
        return WorkerContext(self.backend, self.job_root, self.base, self.worker, args, self.cache if cache is None else cache)

    def measure(self, ids: list[str] | None = None) -> dict[str, Any]:
        """按需求单的判定测当前状态（与 check、round 同一套定义），不存证据。/ Measure with the demand's checks, no evidence kept."""
        from saos import demand

        return demand.quick(self, ids)

    def cancel_requested(self) -> bool:
        own = self.args.get("cancel_file")
        return self.job.cancel_requested() or bool(own and Path(own).is_file())

    def mark_dirty(self) -> None:
        pass


def _emit(message: dict[str, Any]) -> None:
    sys.stdout.write(PREFIX + json.dumps(jsonable(message), ensure_ascii=False) + "\n")
    sys.stdout.flush()


def resolve(fn: str, script: Path | None):
    """(脚本路径或模块名, 函数名)。/ (script path or module name, function name)."""
    if script is not None:
        namespace: dict[str, Any] = {"__name__": f"saos_parallel_{script.stem.replace('-', '_')}", "__file__": str(script)}
        exec(compile(script.read_text(encoding="utf-8"), str(script), "exec"), namespace)
        target = namespace.get(fn)
    else:
        module, _, name = fn.rpartition(":")
        target = getattr(importlib.import_module(module), name, None)
    if not callable(target):
        raise SaosError(ErrorCode.BAD_ARGS, f"{fn} is not a function of {script or 'the module'}")
    return target


def worker_main(index: int) -> int:
    """子进程：连接一个 standalone 实例，按标准输入的任务逐个执行。/ Subprocess: one standalone instance, tasks from stdin."""
    from saos.backend import make_backend

    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    started = time.perf_counter()
    try:
        backend = make_backend("standalone", os.environ.get("SAOS_WORKER_BACKEND"))
        backend.start()
    except Exception as exc:  # noqa: BLE001 - 启动失败（如许可证）只让这个 worker 不可用 / a failed start makes only this worker unavailable
        _emit({"ready": False, "error": f"{type(exc).__name__}: {exc}"[:500]})
        return 1
    _emit({"ready": True, "start_s": round(time.perf_counter() - started, 2)})
    functions: dict[tuple[str, str, int], Any] = {}
    loaded_base: str | None = None
    try:
        for line in sys.stdin:
            msg = json.loads(line)
            if msg.get("stop"):
                break
            t0 = time.perf_counter()
            try:
                script = Path(msg["script"]) if msg.get("script") else None
                key = (str(script), msg["fn"], script.stat().st_mtime_ns if script else 0)
                if key not in functions:
                    functions[key] = resolve(msg["fn"], script)
                base = msg["base"]
                wctx = WorkerContext(backend, Path(msg["job"]), Path(base) if base else None, index, msg.get("args") or {})
                if not base:
                    loaded_base = None  # fn 自己加载或搭建，之后不能当作已加载 / fn loads or builds its own
                elif msg.get("reset", True) or base != loaded_base:
                    backend.load(Path(base))
                    loaded_base = base
                result = functions[key](wctx, msg["item"])
                _emit({"i": msg["i"], "ok": True, "result": result, "s": round(time.perf_counter() - t0, 3)})
            except Exception as exc:  # noqa: BLE001 - 单个条目出错不影响其余条目 / one failing item does not stop the others
                _emit({"i": msg["i"], "ok": False, "error": "".join(traceback.format_exception_only(type(exc), exc)).strip()[:1000],
                       "traceback": traceback.format_exc()[-1500:]})
    finally:
        backend.close()
    return 0


def _read(proc: subprocess.Popen) -> dict[str, Any] | None:
    """读到下一条协议行；进程结束时返回 None。/ Read the next protocol line; None when the process ended."""
    for line in proc.stdout:  # type: ignore[union-attr]
        if line.startswith(PREFIX):
            return json.loads(line[len(PREFIX):])
    return None


class _Worker:
    def __init__(self, index: int, backend: str):
        self.index = index
        self.error: str | None = None
        self.start_s: float | None = None
        self.items = 0
        flags = BELOW_NORMAL if os.name == "nt" else 0
        self.proc = subprocess.Popen([sys.executable, "-m", "saos.parallel", str(index)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, encoding="utf-8", creationflags=flags,
                                     env={**os.environ, "SAOS_WORKER_BACKEND": backend})

    def wait_ready(self) -> bool:
        hello = _read(self.proc)
        if not hello or not hello.get("ready"):
            self.error = (hello or {}).get("error", "the worker exited before it was ready")
            self.stop()
            return False
        self.start_s = hello.get("start_s")
        return True

    @property
    def alive(self) -> bool:
        return self.error is None and self.proc.poll() is None

    def ask(self, message: dict[str, Any]) -> dict[str, Any] | None:
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")  # type: ignore[union-attr]
        self.proc.stdin.flush()  # type: ignore[union-attr]
        return _read(self.proc)

    def stop(self) -> None:
        try:
            self.proc.stdin.write(json.dumps({"stop": True}) + "\n")  # type: ignore[union-attr]
            self.proc.stdin.close()  # type: ignore[union-attr]
        except (OSError, ValueError):
            pass
        try:
            self.proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class Pool:
    """后台进程持有的 worker 池。/ The worker pool held by the daemon."""

    def __init__(self) -> None:
        self.workers: list[_Worker] = []
        self.refused: list[str] = []
        self.cap: int | None = None  # 许可证拒绝过之后不再超过的规模 / the size not exceeded again after a licence refusal
        self.lock = threading.Lock()
        self.timer: threading.Timer | None = None
        self.next_index = 0
        self.backend = config.backend_name()

    def status(self) -> dict[str, Any]:
        return {"workers": sum(1 for w in self.workers if w.alive), "cap": self.cap, "refused": self.refused[-3:], "backend": self.backend}

    def select_backend(self, backend: str) -> None:
        if backend != self.backend:
            self.close()
            self.refused.clear()
            self.backend = backend

    def ensure(self, count: int) -> dict[str, Any]:
        """保证至少 count 个可用 worker（许可证允许的范围内），并发启动缺的那些。
        Make sure count workers are available (as far as the licence allows); missing ones start concurrently."""
        self._cancel_timer()
        self.workers = [w for w in self.workers if w.alive]
        wanted = count if self.cap is None else min(count, self.cap)
        new = [_Worker(self._take_index(), self.backend) for _ in range(max(0, wanted - len(self.workers)))]
        started = time.perf_counter()
        ready: dict[int, bool] = {}
        threads = [threading.Thread(target=lambda w=w: ready.__setitem__(w.index, w.wait_ready()), daemon=True) for w in new]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        refused = [w.error for w in new if not ready.get(w.index)]
        if refused:
            self.refused.extend(e or "" for e in refused)
            self.cap = len(self.workers) + sum(1 for w in new if ready.get(w.index))
        self.workers.extend(w for w in new if ready.get(w.index))
        return {"started": [{"worker": w.index, "start_s": w.start_s} for w in new if ready.get(w.index)],
                "unavailable": refused, "start_wall_s": round(time.perf_counter() - started, 2) if new else 0.0}

    def _take_index(self) -> int:
        self.next_index += 1
        return self.next_index - 1

    def _cancel_timer(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None

    def touch(self) -> None:
        """一次调用结束后重新计时空闲关闭。/ Restart the idle countdown after a call."""
        self._cancel_timer()
        if self.workers:
            self.timer = threading.Timer(idle_seconds(), self._idle_close)
            self.timer.daemon = True
            self.timer.start()

    def _idle_close(self) -> None:
        if self.lock.acquire(blocking=False):
            try:
                self.close()
            finally:
                self.lock.release()

    def close(self) -> None:
        self._cancel_timer()
        workers, self.workers = self.workers, []
        threads = [threading.Thread(target=w.stop, daemon=True) for w in workers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.cap = None


def run_map(ctx, fn: str, items: list[Any], workers: int | None = None, reset: bool = True, args: dict[str, Any] | None = None,
            archive: bool = True) -> dict[str, Any]:
    """把 items 分给池里的 worker 并发执行 fn(w, item)，按条目顺序返回结果。
    Run fn(w, item) over items on the pool's workers at once; results come back in item order."""
    if ctx.lens is None:
        raise SaosError(ErrorCode.NO_LENS_LOADED, f"job {ctx.job.id} has no lens loaded")
    if ctx.dirty:
        raise SaosError(ErrorCode.UNSAVED_CHANGES, "parallel workers load the saved lens file; save your changes first (lens save or ctx.save_as)")
    if not isinstance(items, list) or not items:
        raise SaosError(ErrorCode.BAD_ARGS, "items must be a non-empty list of JSON-able values")
    script: Path | None = None
    if ":" in fn and fn.rpartition(":")[0].endswith(".py"):
        rel, _, fn = fn.rpartition(":")
        script = ctx.path(rel)
    elif ":" in fn:
        if not fn.startswith("saos."):
            raise SaosError(ErrorCode.BAD_ARGS, f"{fn}: a module function must be a saos one (saos.scan:point); job code goes in a .py file")
    else:
        script = getattr(ctx, "script", None)
        if script is None:
            raise SaosError(ErrorCode.BAD_ARGS, f"{fn!r} needs the script it lives in; write 'path/in/job.py:{fn}'")
    if script is not None and not script.is_file():
        raise SaosError(ErrorCode.FILE_NOT_FOUND, f"cannot read {script}")
    if script is not None:
        resolve(fn, script)  # 先在本进程里确认函数存在，免得每个 worker 各报一次 / check once here instead of once per worker
    stages.require_ready(ctx.job.root)
    backend = backend_for(ctx.job)
    count = min(len(items), int(workers or default_workers(backend)), config.compute_cores())
    if count < 1:
        raise SaosError(ErrorCode.BAD_ARGS, "workers must be at least 1")
    folder: Path | None = None
    if archive:
        root = ctx.path(PARALLEL_DIR)
        root.mkdir(exist_ok=True)
        folder = root / f"{jobs.next_archive_number(root):03d}"
        folder.mkdir()
        if script is not None:
            shutil.copy2(script, folder / "work.py")
        write_json(folder / "plan.json", {"fn": fn, "script": None if script is None else script.relative_to(ctx.job.root).as_posix(),
                                          "lens": ctx.job.relative_lens(ctx.lens), "workers": count, "reset": reset,
                                          "args": jsonable(args or {}), "items": jsonable(items)})
    pool: Pool = ctx._daemon.pool
    if not pool.lock.acquire(timeout=2):
        raise SaosError(ErrorCode.POOL_BUSY, "the worker pool is busy with a detached scan; wait for it (saos scan show) or cancel it (saos job cancel)",
                        {"pool": pool.status()})
    try:
        result = dispatch(pool, ctx.job, ctx.lens, fn, script, items, count, reset, args,
                          progress=lambda stage: ctx.job.write_progress(stage=stage), cancelled=ctx.job.cancel_requested)
    finally:
        pool.lock.release()
    result["folder"] = None if folder is None else folder.relative_to(ctx.job.root).as_posix()
    if folder is not None:
        write_json(folder / "result.json", result)
        ctx.job.seal(folder)
    if not result["workers_available"]:
        raise SaosError(ErrorCode.ZOS_ERROR, f"no parallel worker could start: {result['startup']['unavailable']}",
                        {"folder": result["folder"], "startup": result["startup"], "pool": pool.status()})
    return result


def dispatch(pool: Pool, job: jobs.Job, lens: Path | None, fn: str, script: Path | None, items: list[Any], count: int, reset: bool,
             args: dict[str, Any] | None, progress, cancelled) -> dict[str, Any]:
    """在已拿到锁的池上执行：每个 worker 一份 lens 的副本（lens 为 None 时不加载，由 fn 自己加载或搭建）。progress(stage)
    报告进度，cancelled() 为真时在当前条目后停止。不碰后台进程的主实例，所以分离运行的扫描可以放在后台线程里。
    Run on a pool whose lock the caller holds, each worker on its own copy of lens (none loaded when lens is None: fn loads
    or builds its own). progress(stage) reports, cancelled() stops after the current items. It never touches the daemon's
    main instance, so a detached scan can run it in a thread."""
    started = time.perf_counter()
    progress(f"parallel: starting {count} workers")
    pool.select_backend(backend_for(job))
    startup = pool.ensure(count)
    active = [w for w in pool.workers if w.alive][:count]
    temp = Path(tempfile.mkdtemp(prefix="saos-parallel-"))
    pending = list(range(len(items)))
    lock = threading.Lock()
    results: list[Any] = [None] * len(items)
    errors: dict[int, str] = {}
    report: list[dict[str, Any]] = []
    completed = [0]
    stop = threading.Event()

    def drive(worker: _Worker) -> None:
        base = None if lens is None else temp / f"lens-{worker.index}{lens.suffix}"
        if base is not None:
            from saos.lens_files import copy_lens

            copy_lens(lens, base)
        info: dict[str, Any] = {"worker": worker.index, "items": 0, "busy_s": 0.0}
        first = True
        try:
            while not stop.is_set():
                with lock:
                    if not pending:
                        break
                    i = pending.pop(0)
                reply = worker.ask({"i": i, "item": items[i], "fn": fn, "script": None if script is None else str(script),
                                    "base": None if base is None else str(base), "job": str(job.root), "args": args or {},
                                    "reset": reset or first})
                first = False
                if reply is None:
                    with lock:
                        errors[i] = "the worker exited while working on this item"
                    info["died"] = True
                    worker.error = "died"
                    break
                with lock:
                    if reply["ok"]:
                        results[i] = reply["result"]
                    else:
                        errors[i] = reply["error"]
                    info["items"] += 1
                    info["busy_s"] = round(info["busy_s"] + float(reply.get("s") or 0.0), 3)
                    completed[0] += 1
                    progress(f"parallel {completed[0]}/{len(items)}")
                if cancelled():
                    stop.set()
        finally:
            with lock:
                report.append(info)

    threads = [threading.Thread(target=drive, args=(w,), daemon=True) for w in active]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    shutil.rmtree(temp, ignore_errors=True)
    pool.touch()
    report.sort(key=lambda r: r["worker"])
    return {
        "workers": report,
        "workers_available": len(active),
        "startup": startup,
        "results": results,
        "errors": {str(k): v for k, v in sorted(errors.items())},
        "not_run": sorted(pending),
        "cancelled": stop.is_set(),
        "elapsed_s": round(time.perf_counter() - started, 2),
    }


if __name__ == "__main__":
    sys.exit(worker_main(int(sys.argv[1])))
