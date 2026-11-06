"""从任务目录推出工作流走到了哪一步，只读文件，不连接后台进程。
Work out where a job stands in the workflow from its files only; never talks to the daemon.

步骤（与 .agents/skills/saos/SKILL.md 的阶段对应）/ steps:
    interview → demand_confirm（停顿）→ modeling（起型、结构探索、建模）→ model_approve（停顿）→ design → deliver
每一步的状态 / status per step: done, active, waiting（等用户）, blocked（有 blocking 问题）, pending。
"""

from __future__ import annotations

import json
from typing import Any

from saos import client, demand, i18n, jobs, ledger, model_gate
from saos.protocol import ErrorCode, SaosError, finite

STEPS = ("interview", "demand_confirm", "modeling", "model_approve", "design", "deliver")
FINAL = ("achieved", "exhausted", "limit", "stopped")


def _daemon() -> dict[str, Any]:
    state = client.read_state()
    if state is None or not client.pid_alive(int(state.get("pid", 0))):
        return {"running": False, "backend": None}
    # 当前在哪个后端上算、是否两个后端都有（ADR-0018）：窗口据此标明数值来源和 OpticStudio 是否占着许可
    # which backend computes now and whether the daemon holds both: the window labels where numbers come from
    backends = state.get("backends") or {}
    backend = backends.get("active") or (state.get("opticstudio") or {}).get("backend") or "zos"
    return {"running": True, "pid": state.get("pid"), "backend": backend,
            "dual": bool(backends.get("dual")), "zos_started": bool(backends.get("zos_started", backend != "rtc"))}


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
                           "petzval": p.get("petzval"), "seidel": list(((p.get("third_order") or {}).get("totals") or {}).values()),
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


def rounds_of(job: jobs.Job) -> list[dict[str, Any]]:
    """轮次列表，带每轮各需求条目的测量值（“指标”页用）。
    The rounds with each round's measured demand item values (for the Metrics tab)."""
    out = []
    try:
        current_demand = demand.load_job(job.root)
    except SaosError:
        current_demand = None
    for r in ledger.load(job.root / ledger.LEDGER_FILE, current_demand):
        evaluation = r.get("evaluation") or ""
        folder = evaluation.rsplit("/", 1)[0] if evaluation else ""
        out.append({"round": r["round"], "tiers": ledger.tiers_of(r), "note": r.get("note", ""), "shortfall": finite(r["shortfall"]),
                    "achieved": bool(r.get("achieved")), "deliverable": ledger.deliverable(r), "time": r.get("time"),
                    "demand_sha": ledger.version(r)[0], "viz": bool(folder) and (job.root / folder / "viz.json").is_file(),
                    # 谁算的这一轮；rtc 轮次复测后数值换成 OpticStudio 的（ADR-0017）/ who computed it; confirmed rtc rounds show OpticStudio's numbers
                    "backend": r.get("backend", "zos"),
                    **({"confirmed_by": r["confirmed_by"], "agrees": bool(r.get("agrees"))} if r.get("confirmed_by") is not None else {}),
                    "values": {k: finite(v) for k, v in (r.get("values") or {}).items()},
                    # 未达的条目及未达量（账本按判定记下的，窗口不比界限）；更早的记录没有这一项时为 None
                    # failing items and their shortfalls as judged in the ledger (the window never compares bounds); None on older records
                    "failing": ({k: finite(v) for k, v in r["failing"].items()} if isinstance(r.get("failing"), dict) else None)})
    return out


def metric_items(d: dict[str, Any], metrics: list[str] | None) -> list[dict[str, Any]]:
    """“指标”页各条目的说明、单位和界限，按 --metrics 的顺序，没选时为需求里所有带判定方法的条目；需求里已没有的条目标出来。
    Description, unit and bound of each Metrics item in --metrics order, or of every checked demand item when none
    were chosen; items gone from the demand are flagged."""
    if metrics is None:
        metrics = [s["id"] for s in demand.checked(d)]
    entries = {e["id"]: (section, e) for section in demand.SECTIONS for e in d.get(section) or []}
    out = []
    for k in metrics:
        section, e = entries.get(k, (None, {}))
        bound = e.get("goal") or e.get("must") or e.get("range") or ({"equals": e["value"]} if section == "constants" and "value" in e else None)
        out.append({"id": k, "section": section, "missing": section is None, "description": e.get("description"), "unit": e.get("unit"),
                    "bound": bound, "direction": "minimize" if e.get("minimize") is not None else "maximize" if e.get("maximize") is not None else None})
    return out


def _report_current(root) -> bool:
    """报告是否比它依据的记录新：轮次、停止、排除层级，以及报告引用的最近一次完成的公差扫描。只记停止或排除时 ledger 不变，
    所以只比 ledger 会把过时的报告当成已交付。
    Whether the report is newer than what it is built from: rounds, stops, ruled-out tiers and the latest finished
    tolerance scan it cites. A stop or a rule-out leaves the ledger untouched, so comparing with the ledger alone would
    take a stale report for a delivery."""
    report = root / "report" / "index.html"
    if not report.is_file():
        return False
    inputs = [root / name for name in (ledger.LEDGER_FILE, ledger.STOPS_FILE, ledger.RULED_OUT_FILE)]
    scans = root / "scans"
    # 与 report.latest_tolerance 选同一次扫描 / the same scan report.latest_tolerance picks
    for folder in sorted((p for p in scans.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True) if scans.is_dir() else ():
        result = folder / "result.json"
        try:
            if not result.is_file() or "tolerance" not in json.loads((folder / "plan.json").read_text(encoding="utf-8")):
                continue
            data = json.loads(result.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("mode") == "tolerance" and not data.get("cancelled"):
            inputs.append(result)
            break
    made = report.stat().st_mtime
    return all(made >= p.stat().st_mtime for p in inputs if p.is_file())


def pending_revisions(job: jobs.Job, sha: str, demand_confirmed: bool) -> list[dict[str, Any]]:
    """用户在窗口里对当前版本提出、还没处理的修改意见：需求单还是那一版且未确认，或建模说明还是最新一份且未确认。
    出了新版本（或用户后来还是确认了），请求就不再列出。
    Change requests the user made on the current versions and not yet answered: the same demand version, unconfirmed,
    or the latest description, unapproved. A new version (or a later confirmation) settles them."""
    root = job.root
    models = sorted(p for p in (root / "models").glob("*/model.json")) if (root / "models").is_dir() else []
    latest = models[-1].parent if models else None
    open_targets = set()
    if not demand_confirmed:
        open_targets.add(("demand", sha))
    if latest is not None and not (latest / model_gate.APPROVAL_FILE).is_file():
        open_targets.add(("model", latest.relative_to(root).as_posix()))
    return [r for r in jobs.revisions(root) if (r.get("gate"), r.get("target")) in open_targets]


def snapshot(job: jobs.Job) -> dict[str, Any]:
    root = job.root
    status = dict.fromkeys(STEPS, "pending")
    detail: dict[str, Any] = {}
    job_state = job.read_state()
    # 任务记住的窗口设置（saos ui open --tabs / --metrics）/ the job's remembered window settings
    ui = job_state.get("ui") or {}
    out: dict[str, Any] = {"job": job.id, "name": job_state.get("name"), "mode": job.mode, "daemon": _daemon(), "progress": None,
                           "blocking": [], "decision": None, "rounds": rounds_of(job), "ui": ui, "revisions": []}
    # 当前镜头的评价函数值和各项测量值，后台进程自动更新（saos.live）/ the live metrics, updated by the daemon
    try:
        out["metrics"] = json.loads((root / jobs.METRICS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        out["metrics"] = None
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

    # 可行性记录（need.md：explore/feasibility/README.md）写了没有 / whether the feasibility record exists
    out["feasibility"] = (root / "explore" / "feasibility" / "README.md").is_file()
    try:
        d = demand.load_job(root)
    except SaosError as exc:
        status["interview"] = "active"
        detail["interview"] = "no_demand" if exc.code == ErrorCode.FILE_NOT_FOUND else "invalid_demand"
        out["lang"] = i18n.resolve()
        return {**out, "steps": [{"id": s, "status": status[s], "detail": detail.get(s)} for s in STEPS], "current": "interview"}

    out["lang"] = i18n.resolve(d.get("lang"))
    out["metric_items"] = metric_items(d, ui.get("metrics"))
    try:
        jobs.require_zos_history(root)
    except SaosError as exc:
        status["design"] = "blocked"
        out["blocking"] = [{"question": exc.message, "blocking": True}]
        return {**out, "steps": [{"id": key, "status": status[key], "detail": {"legacy": True} if key == "design" else None}
                                for key in STEPS], "current": "design"}
    sha = demand.digest(d)
    # 版本（轮次、停止、排除、报告）用计算层哈希；确认用确认层哈希 / versions use the compute layer; confirmation uses the confirmation layer
    csha = demand.compute_digest(d)
    status["interview"] = "done"
    confirmed = demand.confirmation_for(root, sha, d) is not None
    out["revisions"] = pending_revisions(job, sha, confirmed)
    if not confirmed:
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
            rows = ledger.load(root / ledger.LEDGER_FILE, d)
            in_version = [r for r in ledger.current_window(rows) if ledger.version(r)[0] == csha] if rows else []
            decision = None
            if in_version:
                decision = ledger.decide(rows, d["stop"], ledger.stop_for(root, csha, d), ledger.ruled_out_for(root, csha, d))
                out["decision"] = {k: finite(v) if isinstance(v, float) else v for k, v in decision.items() if k != "stopped"}
            detail["design"] = {"rounds": len(in_version), "last": out["rounds"][-1] if in_version else None,
                                "rtc_unclosed": bool(job_state.get("rtc_unclosed"))}
            # 最近一次复测与 rtc 结论不同、之后还没有新轮次：回到设计 / the latest confirmation disagreed and no round followed
            if decision is not None and "confirm_failed" in decision:
                detail["design"]["confirm_failed"] = decision["confirm_failed"]
            if decision is not None and decision["status"] in FINAL:
                status["design"] = "done"
                status["deliver"] = "done" if _report_current(root) else "active"
                current = "deliver"
                detail["deliver"] = decision["status"]
                shown = ledger.deliver_round(ledger.current_window(rows))
                if not ledger.confirmed(shown):
                    # rtc 轮次待在 OpticStudio 上复测 / an rtc round awaits its OpticStudio re-measurement
                    out["confirm_pending"] = shown["round"]
            else:
                status["design"] = "active"
                current = "design"
        if ledger.stopped_before_rounds(root, csha, d) is not None:
            # 没有结构过门禁就停了：不再等确认，直接到收尾 / stopped with no structure through the gate: nothing to approve, on to the end
            status["modeling"] = "done"
            status["model_approve"] = status["design"] = "pending"
            status["deliver"] = "done" if _report_current(root) else "active"
            current = "deliver"
            detail["deliver"] = "stopped"
            out["no_design"] = True
        blocking = demand.blocking_questions(d)
        if blocking and status[current] in ("active", "waiting"):
            status[current] = "blocked"
            out["blocking"] = blocking
    return {**out, "steps": [{"id": s, "status": status[s], "detail": detail.get(s)} for s in STEPS], "current": current}
