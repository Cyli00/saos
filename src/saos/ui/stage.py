"""从任务目录推出工作流走到了哪一步，只读文件，不连接后台进程。
Work out where a job stands in the workflow from its files only; never talks to the daemon.

步骤（DESIGN.md「工作方式」）/ steps:
    interview → demand_confirm（停顿）→ modeling（起型、结构探索、建模）→ model_approve（停顿）→ design → deliver
每一步的状态 / status per step: done, active, waiting（等用户）, blocked（有 blocking 问题）, pending。
"""

from __future__ import annotations

import json
import math
from typing import Any

from saos import client, demand, i18n, jobs, ledger, model_gate, stages
from saos.protocol import ErrorCode, SaosError

STEPS = ("interview", "demand_confirm", "modeling", "model_approve", "design", "deliver", "zos", "final_report")
FINAL = ("achieved", "exhausted", "limit", "stopped")


def _num(x: Any) -> float | None:
    return x if isinstance(x, (int, float)) and math.isfinite(x) else None


def _daemon() -> dict[str, Any]:
    state = client.read_state()
    if state is None or not client.pid_alive(int(state.get("pid", 0))):
        return {"running": False, "mode": None}
    return {"running": True, "mode": state.get("mode"), "pid": state.get("pid")}


def exploration_of(job: jobs.Job) -> dict[str, Any] | None:
    """读取最新候选初筛的完成状态和保留数量。"""
    root = job.root / "scans"
    plans = sorted(root.glob("*/plan.json")) if root.is_dir() else []
    for plan_file in reversed(plans):
        try:
            plan = json.loads(plan_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if plan.get("mode") != "candidates":
            continue
        out: dict[str, Any] = {"folder": plan_file.parent.relative_to(job.root).as_posix(), "candidates": len(plan.get("candidates") or []),
                               "finished": False, "kept": None}
        try:
            result = json.loads((plan_file.parent / "result.json").read_text(encoding="utf-8"))
            out.update(finished=True, kept=len(result.get("kept") or []))
        except (OSError, ValueError):
            pass
        return out
    return None


def starts_of(job: jobs.Job) -> dict[str, Any] | None:
    for path in sorted((job.root / "explore" / "starts").glob("*/generated.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {"count": data["count"], "not_evaluated": len(data.get("not_evaluated", [])),
                    "folder": path.parent.relative_to(job.root).as_posix()}
        except (OSError, ValueError, KeyError):
            continue
    return None


def starts_detail(job: jobs.Job) -> dict[str, Any]:
    """起点页的数据：最近一次 starts.generate 的候选（来源、缩放、超范围项、预判）和用同一批起点跑的初筛结果，只读已有文件。
    The starts page: the latest generated candidates (source, scale, exceeded ranges, prediction) and the screening scans
    run from them, read from existing files only."""
    paths = sorted((job.root / "explore" / "starts").glob("*/generated.json"), reverse=True)
    if not paths:
        return {"generated": None, "scans": []}
    folder = paths[0].parent
    data = json.loads(paths[0].read_text(encoding="utf-8"))
    candidates = []
    for c in data.get("candidates", []):
        p = c.get("prediction") or {}
        third = (p.get("third_order") or {}).get("totals") or {}
        candidates.append({"id": c["id"], "form": c.get("form"), "provenance": c.get("provenance") or {},
                           "constraints": c.get("constraints") or {}, "first_order": p.get("first_order"),
                           "petzval": p.get("petzval"), "seidel": (p.get("seidel") or {}).get("totals"),
                           "chromatic": {k: third.get(k) for k in ("LchC", "TchC") if k in third},
                           "incidence_max": max((s["max_deg"] for s in (p.get("incidence") or {}).get("surfaces", []) if s.get("max_deg") is not None), default=None),
                           "errors": p.get("errors") or {}})
    rel = folder.relative_to(job.root).as_posix()
    scans = []
    for plan_file in sorted((job.root / "scans").glob("*/plan.json")) if (job.root / "scans").is_dir() else []:
        try:
            plan = json.loads(plan_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if plan.get("generation") != rel:
            continue
        result_file = plan_file.parent / "result.json"
        rows = []
        if result_file.is_file():
            try:
                result = json.loads(result_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                result = {}
            rows = [{k: r.get(k) for k in ("id", "verdict", "valid", "shortfall", "start_shortfall", "problems", "numeric_warnings",
                                           "still_improving", "kept", "error")} for r in result.get("rows", [])]
        stages_ = plan.get("stages") or []
        scans.append({"folder": plan_file.parent.relative_to(job.root).as_posix(), "finished": result_file.is_file(),
                      "budget_s": sum(float(next(iter(s.values())).get("max_seconds", 0)) for s in stages_),
                      "optimised": bool(stages_), "rows": rows,
                      "page": (plan_file.parent / "index.html").relative_to(job.root).as_posix() if (plan_file.parent / "index.html").is_file() else None})
    return {"generated": {"folder": rel, "constraints": data.get("constraints"), "allow_extrapolation": data.get("allow_extrapolation"),
                          "excluded": data.get("excluded", []), "not_evaluated": data.get("not_evaluated", []), "candidates": candidates},
            "scans": scans}


def rounds_of(job: jobs.Job, metrics: list[str] | None = None) -> list[dict[str, Any]]:
    """轮次列表；metrics 给出时另带这些需求条目在每轮的测量值（“指标”页用）。
    The rounds; with metrics, also each round's measured value of those demand items (for the Metrics tab)."""
    out = []
    try:
        current_demand = demand.load_job(job.root)
    except SaosError:
        current_demand = None
    for r in ledger.load(job.root / ledger.LEDGER_FILE, current_demand):
        evaluation = r.get("evaluation") or ""
        folder = evaluation.rsplit("/", 1)[0] if evaluation else ""
        out.append({"round": r["round"], "tiers": ledger.tiers_of(r), "note": r.get("note", ""), "shortfall": _num(r["shortfall"]),
                    "achieved": bool(r.get("achieved")), "deliverable": ledger.deliverable(r), "time": r.get("time"),
                    "demand_sha": ledger.version(r)[0], "viz": bool(folder) and (job.root / folder / "viz.json").is_file()})
        if metrics:
            values = r.get("values") or {}
            out[-1]["values"] = {k: _num(values.get(k)) for k in metrics}
    return out


def metric_items(d: dict[str, Any], metrics: list[str]) -> list[dict[str, Any]]:
    """“指标”页各条目的说明、单位和界限，按 --metrics 的顺序；需求里已没有的条目标出来。
    Description, unit and bound of each Metrics item in --metrics order; items gone from the demand are flagged."""
    entries = {e["id"]: (section, e) for section in demand.SECTIONS for e in d.get(section) or []}
    out = []
    for k in metrics:
        section, e = entries.get(k, (None, {}))
        bound = e.get("goal") or e.get("must") or e.get("range") or ({"equals": e["value"]} if section == "constants" and "value" in e else None)
        out.append({"id": k, "section": section, "missing": section is None, "description": e.get("description"), "unit": e.get("unit"),
                    "bound": bound, "direction": "minimize" if e.get("minimize") is not None else "maximize" if e.get("maximize") is not None else None})
    return out


def snapshot(job: jobs.Job) -> dict[str, Any]:
    root = job.root
    status = dict.fromkeys(STEPS, "pending")
    detail: dict[str, Any] = {}
    job_state = job.read_state()
    # 任务记住的窗口设置（saos ui open --tabs / --metrics）/ the job's remembered window settings
    ui = job_state.get("ui") or {}
    out: dict[str, Any] = {"job": job.id, "name": job_state.get("name"), "daemon": _daemon(), "progress": None,
                           "blocking": [], "decision": None, "rounds": rounds_of(job, ui.get("metrics")), "ui": ui}
    view = root / jobs.VIEW_FILE
    # 当前镜头视图的版本号：界面看到它变了才重新取 / the live view's version; the window refetches only when it changes
    out["live"] = view.stat().st_mtime_ns if view.is_file() else None
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
    if ui.get("metrics"):
        out["metric_items"] = metric_items(d, ui["metrics"])
    sha = demand.digest(d)
    # 版本（轮次、停止、排除、报告）用计算层哈希；确认用确认层哈希 / versions use the compute layer; confirmation uses the confirmation layer
    csha = demand.compute_digest(d)
    status["interview"] = "done"
    if demand.confirmation_for(root, sha, d) is None:
        status["demand_confirm"] = "waiting"
        # 确认过旧版本：需求改过，要重新确认 / an older version was confirmed: the demand changed and needs confirming again
        detail["demand_confirm"] = "reconfirm" if demand.confirmations(root) else "first"
        current = "demand_confirm"
    else:
        status["demand_confirm"] = "done"
        models = sorted(p for p in (root / "models").glob("*/model.json")) if (root / "models").is_dir() else []
        approved = [m for m in models if (m.parent / model_gate.APPROVAL_FILE).is_file()]
        latest_approved = False
        out["model_evidence"] = []
        needs_description = False
        latest = {}
        try:
            latest = json.loads(models[-1].read_text(encoding="utf-8")) if models else {}
            current_model = latest.get("model")
            live = json.loads(view.read_text(encoding="utf-8")) if view.is_file() else {}
            live_lens, current_lens = live.get("lens"), job.read_state().get("current_lens")
            same_lens = (isinstance(live_lens, str) and isinstance(current_lens, str)
                         and job.resolve_lens(live_lens, must_exist=False) == job.resolve_lens(current_lens, must_exist=False))
            # 说明写完时不会刷新视图；旧视图不能盖过更新的说明，否则未确认的新结构会被当成已确认
            # describe does not refresh the view; a stale view must not override a newer description
            live_is_newer = bool(models) and view.is_file() and view.stat().st_mtime_ns >= models[-1].stat().st_mtime_ns
            if live.get("model") and same_lens and live_is_newer:
                current_model = live["model"]
                env = (latest.get("notes") or {}).get("environment") or []
                needs_description = not latest.get("model") or bool(model_gate.structure_changes(latest["model"], current_model, env))
            if approved and current_model:
                matched = model_gate.require_approved(job, current_model)
                latest_approved = True
                out["model_evidence"] = matched["evidence_problems"]
                if matched["legacy_unsealed"]:
                    out["model_evidence"].append({"path": matched["model"], "code": "LEGACY_UNSEALED"})
            elif approved:
                latest_approved = bool((models[-1].parent / model_gate.APPROVAL_FILE).is_file())
        except SaosError as exc:
            out["model_evidence"] = exc.details.get("problems", [])
        except (OSError, ValueError, KeyError):
            pass
        explored = exploration_of(job)
        generated = starts_of(job)
        if generated is not None:
            detail["modeling"] = {"starts": generated}
        if explored is not None:
            detail.setdefault("modeling", {})["exploration"] = explored
        if needs_description and not latest_approved:
            status["modeling"] = "active"
            detail.setdefault("modeling", {})["needs_description"] = True
            current = "modeling"
        elif not approved and not models:
            status["modeling"] = "active"
            current = "modeling"
        elif models and not latest_approved:
            # 最新一份建模说明还没获确认，包括结构改动后重新出的说明 / the latest description awaits approval, incl. one redone after a structure change
            status["modeling"] = "done"
            status["model_approve"] = "waiting"
            detail["model_approve"] = models[-1].parent.relative_to(root).as_posix()
            current = "model_approve"
        else:
            status["modeling"] = status["model_approve"] = "done"
            rows = stages.rounds(root, ledger.load(root / ledger.LEDGER_FILE, d))
            in_version = [r for r in ledger.current_window(rows) if ledger.version(r)[0] == csha] if rows else []
            decision = None
            if in_version:
                decision = ledger.decide(rows, stages.stop(root, d["stop"]), ledger.stop_for(root, csha, d), ledger.ruled_out_for(root, csha, d))
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
            entry = stages.read(root)
            if entry:
                status["deliver"] = "done"
                detail["deliver"] = entry["optiland_conclusion"]
                completed = stages.completed(root)
                status["zos"] = "done" if completed else "active"
                detail["zos"] = {"state": "zos_complete" if completed else "zos_pending", "round": entry["source_round"]}
                if not completed:
                    status["design"] = "pending"
                    current = "zos"
                elif decision is not None and decision["status"] in FINAL:
                    meta_file = root / stages.REPORT
                    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.is_file() else {}
                    ready = (meta.get("stage") == "zos" and meta.get("last_round") == rows[-1]["round"]
                             and (meta.get("demand_sha"), meta.get("method_sha")) == ledger.version(rows[-1]))
                    status["final_report"] = "done" if ready else "active"
                    detail["final_report"] = decision["status"]
                    current = "final_report"
            elif (root / stages.REPORT).is_file():
                meta = json.loads((root / stages.REPORT).read_text(encoding="utf-8"))
                current_method = demand.method_digest(demand.prepare_scripts(root, d))
                if (rows and meta.get("last_round") == rows[-1]["round"] and meta.get("deliverable")
                        and (meta.get("demand_sha"), meta.get("method_sha")) == (csha, current_method)):
                    status["deliver"] = "done"
                    status["zos"] = "waiting"
                    detail["zos"] = {"state": "zos_accept"}
                    current = "zos"
        blocking = demand.blocking_questions(d)
        if blocking and status[current] in ("active", "waiting"):
            status[current] = "blocked"
            out["blocking"] = blocking
    return {**out, "steps": [{"id": s, "status": status[s], "detail": detail.get(s)} for s in STEPS], "current": current}
