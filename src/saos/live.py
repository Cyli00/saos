"""当前镜头指标文件与正式判定结果的复用（ADR-0019、ADR-0022）。

rtc 目录指标由 live_worker 的完整快照副本异步计算；本模块的 measure 保留 OpticStudio
回复后的限时测量路径。check/round 结果直接记录，带完整目录数据与测量视图版本，不再重测。
"""

from __future__ import annotations

import json
import math
import os
import time
from typing import Any

from saos import demand, jobs, metrics, scripting, workflow
from saos.protocol import SaosError, finite, write_json

BUDGET_ENV = "SAOS_LIVE_S"
DEFAULT_BUDGET_S = 3.0


def budget_s() -> float:
    raw = os.environ.get(BUDGET_ENV, "")
    try:
        value = float(raw) if raw.strip() else DEFAULT_BUDGET_S
    except ValueError:
        return DEFAULT_BUDGET_S
    return max(value, 0.0) if math.isfinite(value) else DEFAULT_BUDGET_S


def read(job: jobs.Job) -> dict[str, Any]:
    try:
        data = json.loads((job.root / jobs.METRICS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"items": {}}
    if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
        return {"items": {}}
    return data


def _entry(value: Any, view: str, source: str, *, gap: Any = None, error: Any = None, duration_s: Any = None) -> dict[str, Any]:
    out = {"value": finite(value), "view": view, "source": source, "t": time.time()}
    if finite(gap) is not None:
        out["gap"] = finite(gap)
    if error:
        out["error"] = str(error)[:300]
    if finite(duration_s) is not None:
        out["duration_s"] = finite(duration_s)
    return out


def record(job: jobs.Job, evaluation: dict[str, Any], *, view: str, source: str) -> None:
    from saos.live_worker import WRITE_LOCK

    with WRITE_LOCK:
        _record(job, evaluation, view=view, source=source)


def _record(job: jobs.Job, evaluation: dict[str, Any], *, view: str, source: str) -> None:
    """saos check（source "check"）或 saos round（"round"）刚测过当前镜头：各项的值写进来。只测了几项（--only）时不动 G。
    saos check or saos round has just measured the current lens: store its values. A partial check (--only) leaves G as it was."""
    data = read(job)
    for item in evaluation.get("items") or []:
        entry = _entry(item.get("value"), view, source, gap=item.get("shortfall"), error=item.get("error"),
                       duration_s=item.get("duration_s"))
        evidence = item.get("evidence") or {}
        if evidence.get("metric") and evidence.get("result"):
            result = json.loads(jobs.inside(job.root, evidence["result"]).read_text(encoding="utf-8"))
            entry = {**result, **entry, "metric_id": result["id"], "id": item["id"], "goal": item.get("goal"),
                     "passed": item.get("passed"), "display_only": False, **metrics.picked(evidence["metric"])}
        data["items"][item["id"]] = entry
    if not evaluation.get("partial"):
        data["shortfall"] = {"value": finite(evaluation.get("shortfall")), "view": view, "source": source}
    data.update(view=view, evidence=evaluation.get("evidence_file"), updated=jobs.now_iso())
    data["live"] = {"status": "recorded", "view": view, "source": source}
    write_json(job.root / jobs.METRICS_FILE, data)


def measure(daemon: Any, job: jobs.Job, view: str) -> dict[str, Any]:
    """在 daemon 当前后端上测评价函数和需求各项，按预算截断，写进 metrics.json 并返回它。判定脚本改动了镜头时
    （与 check 的 lens_modified_by_checks 同一个问题）把镜头标为未保存，并在判定代码改过之前暂停自动测量。
    Measure the merit value and the demand items on the daemon's current backend within the budget, write metrics.json
    and return it. When the check scripts change the lens (what check reports as lens_modified_by_checks), the lens is
    marked unsaved and automatic measurement pauses until the check code changes."""
    backend = daemon.backend
    data = read(job)
    data.pop("error", None)
    data.update(view=view, backend=backend.identity().get("backend"), budget_s=budget_s())
    started = time.monotonic()
    try:
        merit = float(backend.merit_value())
        data["merit"] = finite(merit)
        data["merit_error"] = None if math.isfinite(merit) else "a merit row cannot be computed"
    except Exception as exc:  # noqa: BLE001 - 没有评价函数时只是没有这个值 / no merit function just means no value
        data.update(merit=None, merit_error=f"{type(exc).__name__}: {getattr(exc, 'message', exc)}"[:300])
    try:
        d, scripts = demand._prepared(job.root)
    except SaosError as exc:
        data["error"] = exc.message[:300]
        return _write(job, data, started)
    budget = data["budget_s"]
    method = demand.method_digest(scripts)
    if budget <= 0 or (data.get("paused") or {}).get("method_sha") == method:
        return _write(job, data, started)
    data.pop("paused", None)
    specs = demand.checked(d)
    items = data["items"]
    # 单项就超过预算的只等 check 和 round；其余按上次测的时间排，没测过和上次没轮到的在前
    # items that alone exceed the budget wait for check and round; the rest go oldest first
    slow = [s["id"] for s in specs if (finite((items.get(s["id"]) or {}).get("duration_s")) or 0.0) > budget]
    order = sorted((s["id"] for s in specs if s["id"] not in slow), key=lambda i: (items.get(i) or {}).get("t", 0.0))
    before = backend.surfaces()
    with backend.activity("saos live metrics", True):
        res = demand.quick(scripting.ScriptContext(daemon, job, {}), order, deadline=started + budget)
    changed = workflow.lens_changes(before, backend.surfaces())
    if changed:
        if daemon.loaded is not None:
            daemon.loaded.dirty = True
        data["paused"] = {"method_sha": method, "changes": changed[:30]}
    for item_id, value in res["values"].items():
        result = res.get("metric_results", {}).get(item_id, {})
        items[item_id] = {**result, **_entry(value, view, "live", gap=res["gaps"].get(item_id), error=res["errors"].get(item_id),
                                          duration_s=res["durations"].get(item_id))}
        if result:
            spec = next(s for s in specs if s["id"] == item_id)
            items[item_id].update(id=item_id, metric_id=result["id"], goal=spec["goal"], display_only=False,
                                  **metrics.picked(spec["check"]["metric"]))
            if spec["goal"] is not None:
                items[item_id]["passed"] = res["gaps"].get(item_id) == 0
    from saos import live_worker

    for extra in live_worker.selection(job):
        if time.monotonic() >= started + budget:
            break
        ctx = scripting.ScriptContext(daemon, job, {})
        try:
            result = metrics.measure(ctx, extra["id"], **extra.get("params", {}))
            items[extra["key"]] = {**result, **_entry(metrics.scalar(result, extra), view, "live"),
                                    "id": extra["key"], "metric_id": result["id"], "display_only": True, **metrics.picked(extra)}
        except Exception as exc:
            if not backend.healthy():
                raise
            items[extra["key"]] = {**_entry(None, view, "live", error=exc), "id": extra["key"],
                                    "metric_id": extra["id"], "display_only": True, **metrics.picked(extra)}
    pursued = [s["id"] for s in specs if s["goal"] is not None or "direction" in s]
    if all(i in res["values"] for i in pursued):
        data["shortfall"] = {"value": finite(res["shortfall"]), "view": view, "source": "live"}
    data.update(slow=slow, skipped=res["skipped"])
    return _write(job, data, started)


def _write(job: jobs.Job, data: dict[str, Any], started: float) -> dict[str, Any]:
    data.update(updated=jobs.now_iso(), elapsed_s=round(time.monotonic() - started, 3))
    write_json(job.root / jobs.METRICS_FILE, data)
    return data
