"""并发评估：几个 standalone OpticStudio 实例同时调用 ZOS-API（ADR-0008）。
Concurrent evaluation: several standalone OpticStudio instances call ZOS-API at the same time (ADR-0008).

在 saos exec 脚本里调用 / call from a saos exec script:

    def run(ctx):
        return ctx.parallel("explore/scan/work.py", items=[{"r1": 50.0}, {"r1": 51.0}], workers=4)

work.py 定义 work(wctx, item)，返回可 JSON 化的结果；wctx 有 system、zosapi、backend、path、worker、reload。
每个 worker 是一个子进程，开一个自己的 standalone 实例，加载当前镜头（已保存的文件）的副本，从队列中依次领取条目；
默认每个条目开始前重新加载副本，条目之间互不影响。许可证拒绝的 worker 记为不可用，其余照常工作。
计划、脚本副本和结果存到任务的 parallel/NNN/ 并封存。
work.py defines work(wctx, item) returning JSON-able data. Each worker is a subprocess with its own standalone
instance and a copy of the saved current lens; it takes items from a shared queue. By default the copy is reloaded
before every item, so items do not affect each other. A worker refused by the licence is reported as unavailable.

实测（2026-10-15，本机，Cooke 样例逐次改半径取 RSCE）：1、2、4 个实例分别 88.7、161、304 次/秒，启动约 5–6.5 s；
许可证上限为同时 8 个实例（含手动打开的窗口）：已开 3 个时，再开 6 或 8 个都只有 5 个拿到 ZOS-API 许可。
Measured (2026-10-15, this machine): 1, 2 and 4 instances gave 88.7, 161 and 304 evaluations/s, start-up 5–6.5 s;
the licence allows 8 instances at a time, open windows included: with 3 open, only 5 of 6 or 8 new ones got ZOS-API.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from saos import config, jobs
from saos.protocol import ErrorCode, SaosError, jsonable, write_json

PARALLEL_DIR = "parallel"
DEFAULT_WORKERS = 4
# worker 的协议行带前缀，和 OpticStudio 自己打到标准输出的文字区分开（实测会打印 "*** FRU__delta_init() …"）
# protocol lines carry a prefix, apart from what OpticStudio itself prints to stdout (e.g. "*** FRU__delta_init() …")
PREFIX = "@@saos "
START_TIMEOUT_S = 120


class WorkerContext:
    """worker 里 work(wctx, item) 能用的入口。/ What work(wctx, item) can use inside a worker."""

    def __init__(self, backend, job_root: Path, base: Path, index: int):
        self.backend = backend
        self.job_root = job_root
        self.base = base
        self.worker = index

    @property
    def system(self):
        return self.backend.system

    @property
    def zosapi(self):
        from saos.backend.zos import _load_zosapi

        return _load_zosapi(self.backend.root)

    def reload(self) -> None:
        """重新加载这个 worker 的镜头副本。/ Reload this worker's copy of the lens."""
        self.backend.load(self.base)

    def path(self, rel: str) -> Path:
        path = (self.job_root / rel).resolve()
        if self.job_root.resolve() not in path.parents:
            raise SaosError(ErrorCode.PATH_OUTSIDE_JOB, f"path escapes the job directory: {rel}")
        return path


def _emit(message: dict[str, Any]) -> None:
    sys.stdout.write(PREFIX + json.dumps(jsonable(message), ensure_ascii=False) + "\n")
    sys.stdout.flush()


def worker_main(script: str, base: str, job_root: str, index: int, reset: bool) -> int:
    """子进程：连接一个 standalone 实例，按标准输入的条目逐个执行 work(wctx, item)。"""
    from saos.backend import make_backend

    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    started = time.perf_counter()
    try:
        backend = make_backend("standalone")
        backend.start()
        backend.load(Path(base))
        namespace: dict[str, Any] = {"__name__": f"saos_parallel_work_{index}"}
        exec(compile(Path(script).read_text(encoding="utf-8"), script, "exec"), namespace)
        work = namespace.get("work")
        if not callable(work):
            raise SaosError(ErrorCode.BAD_ARGS, "the parallel script must define work(wctx, item)")
    except Exception as exc:  # noqa: BLE001 - 启动失败（如许可证）只让这个 worker 不可用 / a failed start makes only this worker unavailable
        _emit({"ready": False, "error": f"{type(exc).__name__}: {exc}"[:500]})
        return 1
    _emit({"ready": True, "start_s": round(time.perf_counter() - started, 2)})
    wctx = WorkerContext(backend, Path(job_root), Path(base), index)
    try:
        for line in sys.stdin:
            msg = json.loads(line)
            if msg.get("stop"):
                break
            t0 = time.perf_counter()
            try:
                if reset:
                    wctx.reload()
                _emit({"i": msg["i"], "ok": True, "result": work(wctx, msg["item"]), "s": round(time.perf_counter() - t0, 3)})
            except Exception as exc:  # noqa: BLE001 - 单个条目出错不影响其余条目 / one failing item does not stop the others
                _emit({"i": msg["i"], "ok": False, "error": "".join(traceback.format_exception_only(type(exc), exc)).strip()[:1000]})
    finally:
        backend.close()
    return 0


def _read(proc: subprocess.Popen) -> dict[str, Any] | None:
    """读到下一条协议行；进程结束时返回 None。/ Read the next protocol line; None when the process ended."""
    for line in proc.stdout:  # type: ignore[union-attr]
        if line.startswith(PREFIX):
            return json.loads(line[len(PREFIX):])
    return None


def run(ctx, script: str, items: list[Any], workers: int | None = None, reset: bool = True) -> dict[str, Any]:
    """把 items 分给几个 standalone 实例并发执行 work(wctx, item)，按条目顺序返回结果。
    Run work(wctx, item) over items in several standalone instances at once; results come back in item order."""
    if ctx.lens is None:
        raise SaosError(ErrorCode.NO_LENS_LOADED, f"job {ctx.job.id} has no lens loaded")
    if ctx.dirty:
        raise SaosError(ErrorCode.UNSAVED_CHANGES, "parallel workers load the saved lens file; save your changes first (lens save or ctx.save_as)")
    if not isinstance(items, list) or not items:
        raise SaosError(ErrorCode.BAD_ARGS, "items must be a non-empty list of JSON-able values")
    source = ctx.path(script)
    if not source.is_file():
        raise SaosError(ErrorCode.FILE_NOT_FOUND, f"cannot read {script}")
    count = min(len(items), int(workers or DEFAULT_WORKERS), config.compute_cores())
    if count < 1:
        raise SaosError(ErrorCode.BAD_ARGS, "workers must be at least 1")
    root = ctx.path(PARALLEL_DIR)
    root.mkdir(exist_ok=True)
    folder = root / f"{jobs.next_archive_number(root):03d}"
    folder.mkdir()
    shutil.copy2(source, folder / "work.py")
    write_json(folder / "plan.json", {"script": script, "lens": ctx.job.relative_lens(ctx.lens), "workers": count, "reset": reset,
                                      "items": jsonable(items)})
    temp = Path(tempfile.mkdtemp(prefix="saos-parallel-"))
    pending = list(range(len(items)))
    lock = threading.Lock()
    results: list[Any] = [None] * len(items)
    errors: dict[int, str] = {}
    report: list[dict[str, Any]] = []
    completed = [0]
    cancelled = threading.Event()
    started = time.perf_counter()

    def drive(index: int) -> None:
        base = temp / f"lens-{index}{ctx.lens.suffix}"
        shutil.copy2(ctx.lens, base)
        cmd = [sys.executable, "-m", "saos.parallel", str(folder / "work.py"), str(base), str(ctx.job.root), str(index), "1" if reset else "0"]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
        info: dict[str, Any] = {"worker": index, "items": 0}
        try:
            hello = _read(proc)
            if not hello or not hello.get("ready"):
                info["unavailable"] = (hello or {}).get("error", "the worker exited before it was ready")
                return
            info["start_s"] = hello.get("start_s")
            while not cancelled.is_set():
                with lock:
                    if not pending:
                        break
                    i = pending.pop(0)
                proc.stdin.write(json.dumps({"i": i, "item": items[i]}, ensure_ascii=False) + "\n")  # type: ignore[union-attr]
                proc.stdin.flush()  # type: ignore[union-attr]
                reply = _read(proc)
                if reply is None:
                    with lock:
                        errors[i] = "the worker exited while working on this item"
                    info["died"] = True
                    break
                with lock:
                    if reply["ok"]:
                        results[i] = reply["result"]
                    else:
                        errors[i] = reply["error"]
                    info["items"] += 1
                    completed[0] += 1
                    ctx.job.write_progress(stage=f"parallel {completed[0]}/{len(items)}")
                if ctx.job.cancel_requested():
                    cancelled.set()
        finally:
            try:
                proc.stdin.write(json.dumps({"stop": True}) + "\n")  # type: ignore[union-attr]
                proc.stdin.close()  # type: ignore[union-attr]
            except OSError:
                pass
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
            with lock:
                report.append(info)

    threads = [threading.Thread(target=drive, args=(k,), daemon=True) for k in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    shutil.rmtree(temp, ignore_errors=True)
    not_run = sorted(pending)
    report.sort(key=lambda r: r["worker"])
    result = {
        "folder": folder.relative_to(ctx.job.root).as_posix(),
        "workers": report,
        "workers_available": sum(1 for r in report if "unavailable" not in r),
        "results": results,
        "errors": {str(k): v for k, v in sorted(errors.items())},
        "not_run": not_run,
        "cancelled": cancelled.is_set(),
        "elapsed_s": round(time.perf_counter() - started, 2),
    }
    write_json(folder / "result.json", result)
    ctx.job.seal(folder)
    if not result["workers_available"]:
        reasons = sorted({r.get("unavailable", "") for r in report})
        raise SaosError(ErrorCode.ZOS_ERROR, f"no parallel worker could start: {reasons}", {"folder": result["folder"], "workers": report})
    return result


if __name__ == "__main__":
    sys.exit(worker_main(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5] == "1"))
