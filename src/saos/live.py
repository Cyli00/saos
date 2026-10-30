"""流程图窗口的当前镜头指标（任务的 metrics.json，ADR-0019）：评价函数值和需求每一项的值。

后台进程回复一个改动镜头的操作之后、读下一个请求之前测量，agent 不用调用命令；saos check 和 saos round 测过的值
直接写进来，不再重测。每次测量有时间预算（SAOS_LIVE_S，默认 3 s，0 关闭）：预算用完时没测到的项排到下一次最前，
单项就超过预算的项不再自动测，只用 check 和 round 的值。每个值记下测它时的视图版本（view.json 的 view），
界面据此把镜头改动之前的值标为过时。

The workflow window's live metrics (the job's metrics.json, ADR-0019): the merit value and every demand item's value.
The daemon measures them after replying to an op that changed the lens and before reading the next request, so the
agent runs no command for it; values measured by saos check and saos round are written as they are. Each pass has a
time budget (SAOS_LIVE_S, default 3 s, 0 turns it off): items the budget did not reach go first next time, and an item
that alone takes longer than the budget is left to check and round. Each value carries the view version it was measured
on, so the window can mark values from before a lens change as stale.
"""

from __future__ import annotations

import json
import math
import os
import time
from typing import Any

from saos import demand, jobs, scripting, workflow
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
    """saos check（source "check"）或 saos round（"round"）刚测过当前镜头：各项的值写进来。只测了几项（--only）时不动 G。
    saos check or saos round has just measured the current lens: store its values. A partial check (--only) leaves G as it was."""
    data = read(job)
    for item in evaluation.get("items") or []:
        data["items"][item["id"]] = _entry(item.get("value"), view, source, gap=item.get("shortfall"), error=item.get("error"),
                                           duration_s=item.get("duration_s"))
    if not evaluation.get("partial"):
        data["shortfall"] = {"value": finite(evaluation.get("shortfall")), "view": view, "source": source}
    data.update(view=view, evidence=evaluation.get("evidence_file"), updated=jobs.now_iso())
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
        items[item_id] = _entry(value, view, "live", gap=res["gaps"].get(item_id), error=res["errors"].get(item_id),
                                duration_s=res["durations"].get(item_id))
    pursued = [s["id"] for s in specs if s["goal"] is not None or "direction" in s]
    if all(i in res["values"] for i in pursued):
        data["shortfall"] = {"value": finite(res["shortfall"]), "view": view, "source": "live"}
    data.update(slow=slow, skipped=res["skipped"])
    return _write(job, data, started)


def _write(job: jobs.Job, data: dict[str, Any], started: float) -> dict[str, Any]:
    data.update(updated=jobs.now_iso(), elapsed_s=round(time.monotonic() - started, 3))
    write_json(job.root / jobs.METRICS_FILE, data)
    return data
