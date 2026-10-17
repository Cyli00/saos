"""从任务目录推出工作流走到了哪一步，只读文件，不连接后台进程。
Work out where a job stands in the workflow from its files only; never talks to the daemon.

步骤（DESIGN.md「工作方式」）/ steps:
    interview → demand_confirm（停顿）→ modeling → model_approve（停顿）→ design → deliver
每一步的状态 / status per step: done, active, waiting（等用户）, blocked（有 blocking 问题）, pending。
"""

from __future__ import annotations

import math
from typing import Any

from saos import client, demand, i18n, jobs, ledger, model_gate
from saos.protocol import ErrorCode, SaosError

STEPS = ("interview", "demand_confirm", "modeling", "model_approve", "design", "deliver")
FINAL = ("achieved", "exhausted", "limit", "stopped")


def _num(x: Any) -> float | None:
    return x if isinstance(x, (int, float)) and math.isfinite(x) else None


def _daemon() -> dict[str, Any]:
    state = client.read_state()
    if state is None or not client.pid_alive(int(state.get("pid", 0))):
        return {"running": False, "mode": None}
    return {"running": True, "mode": state.get("mode"), "pid": state.get("pid")}


def rounds_of(job: jobs.Job) -> list[dict[str, Any]]:
    out = []
    for r in ledger.load(job.root / ledger.LEDGER_FILE):
        evaluation = r.get("evaluation") or ""
        folder = evaluation.rsplit("/", 1)[0] if evaluation else ""
        out.append({"round": r["round"], "tiers": ledger.tiers_of(r), "note": r.get("note", ""), "shortfall": _num(r["shortfall"]),
                    "achieved": bool(r.get("achieved")), "deliverable": ledger.deliverable(r), "time": r.get("time"),
                    "demand_sha": r.get("demand_sha"), "viz": bool(folder) and (job.root / folder / "viz.json").is_file()})
    return out


def snapshot(job: jobs.Job) -> dict[str, Any]:
    root = job.root
    status = dict.fromkeys(STEPS, "pending")
    detail: dict[str, Any] = {}
    out: dict[str, Any] = {"job": job.id, "name": job.read_state().get("name"), "daemon": _daemon(), "progress": None,
                           "blocking": [], "decision": None, "rounds": rounds_of(job)}
    progress = job.read_progress()
    if progress and progress.get("status") == "running":
        out["progress"] = progress
    else:
        # 分离运行的扫描写自己的进度文件 / a detached scan writes its own progress file
        detached = jobs.detached_runs(job.root)
        if detached:
            out["progress"] = {**detached[-1], "detail": detached[-1]["folder"]}

    try:
        d = demand.load_job(root)
    except SaosError as exc:
        status["interview"] = "active"
        detail["interview"] = "no_demand" if exc.code == ErrorCode.FILE_NOT_FOUND else "invalid_demand"
        out["lang"] = i18n.resolve()
        return {**out, "steps": [{"id": s, "status": status[s], "detail": detail.get(s)} for s in STEPS], "current": "interview"}

    out["lang"] = i18n.resolve(d.get("lang"))
    sha = demand.digest(d)
    status["interview"] = "done"
    if demand.confirmation_for(root, sha) is None:
        status["demand_confirm"] = "waiting"
        # 确认过旧版本：需求改过，要重新确认 / an older version was confirmed: the demand changed and needs confirming again
        detail["demand_confirm"] = "reconfirm" if demand.confirmations(root) else "first"
        current = "demand_confirm"
    else:
        status["demand_confirm"] = "done"
        models = sorted(p for p in (root / "models").glob("*/model.json")) if (root / "models").is_dir() else []
        approved = [m for m in models if (m.parent / model_gate.APPROVAL_FILE).is_file()]
        if not approved and not models:
            status["modeling"] = "active"
            current = "modeling"
        elif models and not (models[-1].parent / model_gate.APPROVAL_FILE).is_file():
            # 最新一份建模说明还没获确认，包括结构改动后重新出的说明 / the latest description awaits approval, incl. one redone after a structure change
            status["modeling"] = "done"
            status["model_approve"] = "waiting"
            detail["model_approve"] = models[-1].parent.relative_to(root).as_posix()
            current = "model_approve"
        else:
            status["modeling"] = status["model_approve"] = "done"
            rows = ledger.load(root / ledger.LEDGER_FILE)
            in_version = [r for r in ledger.current_window(rows) if r.get("demand_sha") == sha] if rows else []
            decision = None
            if in_version:
                decision = ledger.decide(rows, d["stop"], ledger.stop_for(root, sha), ledger.ruled_out_for(root, sha))
                out["decision"] = {k: _num(v) if isinstance(v, float) else v for k, v in decision.items() if k != "stopped"}
            detail["design"] = {"rounds": len(in_version), "last": out["rounds"][-1] if in_version else None}
            if decision is not None and decision["status"] in FINAL:
                status["design"] = "done"
                report = root / "report" / "index.html"
                ledger_file = root / ledger.LEDGER_FILE
                if report.is_file() and report.stat().st_mtime >= ledger_file.stat().st_mtime:
                    status["deliver"] = "done"
                    current = "deliver"
                else:
                    status["deliver"] = "active"
                    current = "deliver"
                detail["deliver"] = decision["status"]
            else:
                status["design"] = "active"
                current = "design"
        blocking = demand.blocking_questions(d)
        if blocking and status[current] in ("active", "waiting"):
            status[current] = "blocked"
            out["blocking"] = blocking
    return {**out, "steps": [{"id": s, "status": status[s], "detail": detail.get(s)} for s in STEPS], "current": current}
