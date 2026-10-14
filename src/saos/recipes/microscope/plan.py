"""离线生成可审阅的设计计划，确认绑定完整计划、规格和初始结构快照。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from saos import i18n, jobs

from saos.recipes.microscope import acceptance, interview, library
from saos.recipes.microscope.plan_html import PAGES, render
from saos import protocol
from saos.protocol import ErrorCode, SaosError, read_json
from saos.recipes.microscope.spec import Spec, parse_spec


def digest(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def write_json(path: Path, data: Any) -> None:
    protocol.write_json(path, data, strict=True)


def _draft_job(job: jobs.Job) -> None:
    state = job.read_state()
    if state.get("frozen") or (job.root / "rounds.jsonl").exists() or (job.root / "bench.jsonl").exists():
        raise SaosError(ErrorCode.BAD_ARGS, "a job with optimisation or benchmark records cannot create or confirm a new design plan; create a new job")
    if state.get("plan", {}).get("approved"):
        raise SaosError(ErrorCode.BAD_ARGS, "this job's design plan is already confirmed; create a new job to change the requirements")


def _query(spec: Spec, limit: int) -> list[dict[str, Any]]:
    return library.query(na=spec.na, magnification=spec.magnification, correction_class=spec.correction_class,
                         objective_field_number=spec.objective_field_number, working_distance_min=spec.item("working_distance").threshold,
                         tube_lens_mm=spec.tube_lens_focal_length, limit=limit)


def candidates(result: dict[str, Any]) -> list[dict[str, Any]]:
    return _query(parse_spec(result["spec"]), 5)


def starting_design(entry_id: str, result: dict[str, Any], rationale: str) -> dict[str, Any]:
    if not rationale.strip():
        interview.bad("choosing a starting design needs --rationale: why this structure and what still needs changing")
    entry = library.load_entry(entry_id)
    if entry.get("status") != "admitted" or not entry.get("repro", {}).get("pass"):
        interview.bad("the starting design must be admitted and pass its reproduction check")
    spec = parse_spec(result["spec"])
    match = next(r for r in _query(spec, len(library.all_entries())) if r["id"] == entry_id)
    # 调整项是结构化标记，计划页按语言显示
    notes = list(match["flags"])
    if match["scaled"]["optical_length_mm"] > spec.item("optical_length").threshold:
        notes.append({"kind": "optical_length"})
    if match["scaled"]["clear_diameter_mm"] > spec.item("clear_diameter").threshold:
        notes.append({"kind": "clear_diameter"})
    if entry["metrics"].get("cover_glass") != spec.cover_glass:
        notes.append({"kind": "cover_glass"})
    glass_changes = [g["material"] for g in entry["metrics"].get("glasses", [])
                     if g.get("catalog", "").upper() not in spec.glass_catalogs or g.get("status") not in spec.glass_allowed_status]
    if glass_changes:
        notes.append({"kind": "glass", "materials": glass_changes})
    return {"entry": entry, "query": match, "rationale": rationale, "adjustments": notes,
            "prescription": library.read_json(library.entry_dir(entry_id) / "prescription.json")}


def create(job_id: str, draft_path: Path, entry_id: str | None, rationale: str = "") -> dict[str, Any]:
    job = jobs.get_job(job_id)
    _draft_job(job)
    draft = interview.read_draft(draft_path)
    result = interview.check(draft)
    if not result["ready"]:
        raise SaosError(ErrorCode.BAD_ARGS, "the interview is incomplete; ask the open questions first", {"questions": result["questions"]})
    spec = parse_spec(result["spec"])
    config = acceptance.acceptance_config(spec)
    payload = {"schema": 1, "job": job.id, "created_at": jobs.now_iso(), "interview": draft, "review": result,
               "acceptance_config": config, "candidates": candidates(result),
               "starting_design": starting_design(entry_id, result, rationale) if entry_id else None,
               "state": "awaiting_confirmation" if entry_id else "awaiting_structure"}
    revision = digest(payload)
    directory = job.root / "plan" / revision
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / "plan.json", payload)
    write_json(directory / "spec.json", result["spec"])
    for lang, name in PAGES.items():
        (directory / name).write_text(render(payload, revision, lang), encoding="utf-8")
    artifacts = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in ("plan.json", "spec.json", *PAGES.values())}
    write_json(directory / "artifacts.json", artifacts)
    pointer = {"revision": revision, "approved": False, "state": payload["state"]}
    write_json(job.root / "plan" / "current.json", pointer)
    job.update_state(plan=pointer)
    return {"job": job.id, "revision": revision, "state": payload["state"], **_pages(directory),
            "assumptions": sum(r["source"] == "assumption" for r in result["fields"])}


def _pages(directory: Path) -> dict[str, Any]:
    pages = {lang: str(directory / name) for lang, name in PAGES.items()}
    return {"page": pages[i18n.resolve()], "pages": pages}


def inspect(job_id: str, revision: str | None = None) -> dict[str, Any]:
    job = jobs.get_job(job_id)
    pointer = read_json(job.root / "plan" / "current.json")
    revision = revision or pointer["revision"]
    if not isinstance(revision, str) or len(revision) != 64 or any(c not in "0123456789abcdef" for c in revision):
        interview.bad("the plan revision must be a full SHA-256")
    if revision != pointer["revision"]:
        raise SaosError(ErrorCode.SPEC_CHANGED, "this is not the current design plan; review the latest revision")
    directory = job.root / "plan" / revision
    payload = read_json(directory / "plan.json")
    if digest(payload) != revision or payload.get("job") != job.id:
        raise SaosError(ErrorCode.SPEC_CHANGED, "the design plan has changed; create and review it again")
    artifacts = read_json(directory / "artifacts.json")
    for name in ("plan.json", "spec.json", *PAGES.values()):
        if not (directory / name).is_file() or hashlib.sha256((directory / name).read_bytes()).hexdigest() != artifacts.get(name):
            raise SaosError(ErrorCode.SPEC_CHANGED, f"design plan file {name} has changed; create and review it again")
    if read_json(directory / "spec.json") != payload["review"]["spec"]:
        raise SaosError(ErrorCode.SPEC_CHANGED, "the spec shown in the plan differs from the spec to hand off")
    live_config = acceptance.acceptance_config(parse_spec(payload["review"]["spec"]))
    if live_config != payload["acceptance_config"]:
        raise SaosError(ErrorCode.SPEC_CHANGED, "the current evaluator differs from the plan; create and review it again")
    return {"job": job.id, "revision": revision, "state": pointer["state"], "approved": pointer["approved"],
            **_pages(directory), "payload": payload}


def confirm(job_id: str, revision: str, evidence: str) -> dict[str, Any]:
    if not evidence.strip():
        interview.bad("--evidence needs the user's own words confirming this revision")
    reviewed = inspect(job_id, revision)
    job = jobs.get_job(job_id)
    if reviewed["approved"]:
        return {"job": job_id, "revision": revision, "state": "confirmed", "already_confirmed": True}
    _draft_job(job)
    if reviewed["payload"]["starting_design"] is None:
        interview.bad("no admitted starting design is chosen; admit one offline or choose an existing one, then create the plan again")
    payload = reviewed["payload"]
    config = payload["acceptance_config"]
    spec_path = f"spec/plan-{revision}.json"
    write_json(job.root / spec_path, payload["review"]["spec"])
    approval = {"revision": revision, "approved": True, "state": "confirmed", "confirmed_at": jobs.now_iso(),
                "evidence": evidence, "spec": spec_path, "config_sha256": acceptance.config_hash(config)}
    write_json(job.root / "plan" / revision / "approval.json", approval)
    write_json(job.root / "plan" / "current.json", approval)
    job.update_state(spec=spec_path, plan=approval)
    return {"job": job.id, **approval, "next": "confirmation recorded; prepare the starting lens, then run round 0 acceptance against this spec"}


def handoff(job_id: str) -> dict[str, Any]:
    """自动阶段读取确认记录时，再检查任务规格未被改动。"""
    reviewed = inspect(job_id)
    if not reviewed["approved"]:
        interview.bad("the design plan is not confirmed by the user")
    job = jobs.get_job(job_id)
    approval = read_json(job.root / "plan" / reviewed["revision"] / "approval.json")
    if job.read_state().get("spec") != approval["spec"] or read_json(job.root / approval["spec"]) != reviewed["payload"]["review"]["spec"]:
        raise SaosError(ErrorCode.SPEC_CHANGED, "the job spec has drifted from the design plan the user confirmed")
    return {"job": job_id, "revision": reviewed["revision"], "spec": approval["spec"],
            "config_sha256": approval["config_sha256"], "starting_design": reviewed["payload"]["starting_design"],
            "note": "this prescription is the library snapshot, not yet adapted to the user's spec; its library acceptance is not this job's acceptance"}
