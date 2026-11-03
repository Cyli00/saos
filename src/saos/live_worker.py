"""rtc 实时指标：完整镜头快照、单个计算进程、一个可替换的待算帧。"""

from __future__ import annotations

import json
import logging
import math
import multiprocessing
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from saos import demand, jobs, metrics
from saos.protocol import ErrorCode, SaosError, finite, write_json

log = logging.getLogger("saos.live")
SELECTION_FILE = "metric_selection.json"
WRITE_LOCK = threading.RLock()


def selection(job: jobs.Job) -> list[dict[str, Any]]:
    path = job.root / SELECTION_FILE
    if not path.exists():
        return []
    try:
        return validate_selection(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise SaosError(ErrorCode.BAD_ARGS, f"cannot read {SELECTION_FILE}: {exc}") from exc


def validate_selection(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, list) or len(data) > 64:
        raise SaosError(ErrorCode.BAD_ARGS, "metric selection must be a list of at most 64 entries")
    names = set()
    for item in data:
        if not isinstance(item, dict) or set(item) - {"key", "id", "params", "pick"}:
            raise SaosError(ErrorCode.BAD_ARGS, "each selected metric needs {key, id, params?, pick?}")
        key = item.get("key")
        if not isinstance(key, str) or not demand.ID_RE.fullmatch(key) or key in names:
            raise SaosError(ErrorCode.BAD_ARGS, "selected metric keys must be unique demand-style ids")
        names.add(key)
        metrics.validate({k: v for k, v in item.items() if k != "key"})
    return data


def demand_specs(job: jobs.Job) -> list[dict[str, Any]]:
    """带判定的需求条目；还没有需求单的新任务没有条目，只有旧格式文件时仍由 load_job 报出改写提示。
    The checked demand items; a new job without a demand has none, a legacy-only job still gets load_job's pointer."""
    if not ((job.root / demand.DEMAND_FILE).is_file() or (job.root / demand.LEGACY_FILE).is_file()):
        return []
    return demand.checked(demand.load_job(job.root))


def requests(job: jobs.Job) -> list[dict[str, Any]]:
    specs = demand_specs(job)
    out = [{"key": s["id"], "metric": s["check"]["metric"], "spec": s} for s in specs if "metric" in s["check"]]
    reserved = {s["id"] for s in specs}
    for item in selection(job):
        if item["key"] in reserved:
            raise SaosError(ErrorCode.BAD_ARGS, f"selected metric key {item['key']} conflicts with a demand item")
        out.append({"key": item["key"], "metric": {k: v for k, v in item.items() if k != "key"}})
    return out


def _compute(ctx, frame):
    items = {}
    for request in frame["requests"]:
        spec = request["metric"]
        started = time.monotonic()
        stamp = {"view": frame["view"], "source": "live", "t": time.time(), "iteration": frame.get("iteration")}
        try:
            result = metrics.measure(ctx, spec["id"], **spec.get("params", {}))
            value = metrics.scalar(result, spec)
            entry = {**result, **stamp, "metric_id": result["id"], "id": request["key"], "value": value, **metrics.picked(spec)}
            if "spec" in request:
                goal = request["spec"]["goal"]
                entry["goal"] = goal
                if goal is not None:
                    gap = demand.shortfall(request["spec"], value)
                    entry.update(gap=finite(gap), passed=gap == 0)
            items[request["key"]] = entry
        except Exception as exc:  # 单项失败作为帧数据返回；进程连接与超时由父进程处理。
            metric = metrics.definition(spec["id"])
            items[request["key"]] = {**stamp, "id": request["key"], "metric_id": spec["id"], "value": None,
                                     "name": metric.name, "shape": metric.shape, **metrics.picked(spec),
                                     "error": f"{type(exc).__name__}: {exc}"[:500], "error_kind": demand._error_kind(exc)}
        items[request["key"]]["duration_s"] = round(time.monotonic() - started, 3)
    return items


def _main(connection):
    from saos.backend import make_backend
    from saos.parallel import WorkerContext

    backend = make_backend("standalone", "rtc")
    try:
        backend.start()
        connection.send({"ready": True})
        while True:
            frame = connection.recv()
            if frame is None:
                return
            try:
                backend.load(Path(frame["lens"]))
                backend.select_configuration(frame["configuration"])
                ctx = WorkerContext(backend, Path(frame["job"]), Path(frame["lens"]), 0)
                result = {"items": _compute(ctx, frame)}
                try:
                    result["merit"] = finite(backend.merit_value())
                except Exception as exc:
                    result["merit_error"] = f"{type(exc).__name__}: {exc}"[:300]
                result["provenance"] = backend.provenance()
                connection.send(result)
            except Exception as exc:  # 帧加载失败必须可见，不影响下一份完整快照。
                connection.send({"error": f"{type(exc).__name__}: {exc}"[:500]})
    except (EOFError, BrokenPipeError):
        return
    finally:
        backend.close()
        connection.close()


class LiveWorker:
    def __init__(self):
        self._condition = threading.Condition(WRITE_LOCK)
        self._pending = None
        self._epoch = 0
        self._closed = False
        self._process = None
        self._pipe = None
        self._ready = False
        self._thread = threading.Thread(target=self._run, name="saos-live", daemon=True)
        self._thread.start()

    def invalidate(self):
        with self._condition:
            self._epoch += 1
            if self._pending is not None:
                shutil.rmtree(self._pending["folder"], ignore_errors=True)
                self._pending = None
            self._condition.notify_all()

    def submit(self, backend, job, view, *, iteration=None, merit=None):
        wanted = requests(job)
        folder = Path(tempfile.mkdtemp(prefix="saos-live-"))
        try:
            lens = folder / "snapshot.zmx"
            backend.snapshot_to(lens)
            frame = {"folder": str(folder), "lens": str(lens), "job": str(job.root), "view": view,
                     "iteration": iteration, "merit": finite(merit), "t": time.time(), "requests": wanted,
                     "configuration": backend.current_configuration()}
            with self._condition:
                if self._closed:
                    shutil.rmtree(folder, ignore_errors=True)
                    return
                frame["epoch"] = self._epoch
                if self._pending is not None:
                    shutil.rmtree(self._pending["folder"], ignore_errors=True)
                self._pending = frame
                self._condition.notify_all()
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise

    def _start(self):
        if self._process is not None and self._process.is_alive():
            return
        self._stop()
        context = multiprocessing.get_context("spawn")
        self._pipe, child = context.Pipe()
        self._process = context.Process(target=_main, args=(child,), name="saos-live-rtc", daemon=True)
        self._process.start()
        child.close()

    def _stop(self):
        if self._process is not None:
            if self._process.is_alive():
                self._process.terminate()
            self._process.join(timeout=2)
            if self._process.is_alive():
                self._process.kill()
                self._process.join(timeout=2)
            self._process = None
        if self._pipe is not None:
            self._pipe.close()
            self._pipe = None
        self._ready = False

    def _receive(self, frame, deadline):
        while True:
            with self._condition:
                if self._closed or frame["epoch"] != self._epoch:
                    return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"live frame exceeded {frame['timeout_s']:g} s; deferred to check/round")
            if self._pipe.poll(min(0.1, remaining)):
                return self._pipe.recv()

    def _publish(self, frame, result):
        from saos import live

        job = jobs.Job(Path(frame["job"]).name, Path(frame["job"]))
        with self._condition:
            if self._closed or frame["epoch"] != self._epoch:
                return
            data = live.read(job)
            data.pop("error", None)
            data.update(schema=2, view=frame["view"], backend="rtc", iteration=frame["iteration"],
                        merit=frame["merit"] if frame["merit"] is not None else result.get("merit"),
                        merit_error=result.get("merit_error"), t=frame["t"], updated=jobs.now_iso())
            data["provenance"] = result.get("provenance")
            data["live"] = {"status": "error" if result.get("error") else "ready", "view": frame["view"],
                            "iteration": frame["iteration"], "error": result.get("error"), "timeout_s": frame["timeout_s"]}
            data["live"]["preview"] = True
            wanted = {r["key"] for r in frame["requests"]}
            data["items"] = {k: v for k, v in data["items"].items() if not v.get("display_only") or k in wanted}
            for request in frame["requests"]:
                key = request["key"]
                entry = result.get("items", {}).get(key)
                if entry is None:
                    metric = metrics.definition(request["metric"]["id"])
                    entry = {"id": key, "metric_id": metric.id, "name": metric.name, **metrics.picked(request["metric"]),
                             "shape": metric.shape, "value": None, "view": frame["view"], "source": "live",
                             "t": time.time(), "iteration": frame["iteration"], "error": result.get("error", "frame failed")}
                data["items"][key] = {**entry, "display_only": "spec" not in request}
            write_json(job.root / jobs.METRICS_FILE, data)

    def _run(self):
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._closed or self._pending is not None)
                    if self._closed:
                        return
                    frame, self._pending = self._pending, None
                try:
                    try:
                        timeout = float(os.environ.get("SAOS_LIVE_FRAME_S", "10"))
                    except ValueError:
                        timeout = 10.0
                    timeout = timeout if math.isfinite(timeout) and timeout > 0 else 10.0
                    frame["timeout_s"] = timeout
                    deadline = time.monotonic() + timeout
                    self._start()
                    if not self._ready:
                        ready = self._receive(frame, deadline)
                        if ready is None:
                            self._stop()
                            continue
                        if not ready.get("ready"):
                            raise RuntimeError("live worker failed to start")
                        self._ready = True
                    self._pipe.send(frame)
                    result = self._receive(frame, deadline)
                    if result is not None:
                        self._publish(frame, result)
                    else:
                        self._stop()
                except Exception as exc:  # 实时链路失败写日志及错误帧，不中断主镜头优化。
                    log.exception("实时指标帧计算失败")
                    self._stop()
                    try:
                        self._publish(frame, {"error": f"{type(exc).__name__}: {exc}"[:500]})
                    except Exception:
                        log.exception("实时指标错误帧写入失败")
                finally:
                    shutil.rmtree(frame["folder"], ignore_errors=True)
        finally:
            self._stop()

    def close(self):
        with self._condition:
            self._closed = True
            self.invalidate()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            log.error("实时指标监督线程未在 5 秒内结束")
