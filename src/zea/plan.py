"""离线生成可审阅的设计计划，确认绑定完整计划、规格和初始结构快照。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from zea import acceptance, interview, jobs, library
from zea import protocol
from zea.protocol import ErrorCode, ZeaError, read_json
from zea.spec import Spec, parse_spec


def digest(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def write_json(path: Path, data: Any) -> None:
    protocol.write_json(path, data, strict=True)


def _draft_job(job: jobs.Job) -> None:
    state = job.read_state()
    if state.get("frozen") or (job.root / "rounds.jsonl").exists() or (job.root / "bench.jsonl").exists():
        raise ZeaError(ErrorCode.BAD_ARGS, "已有优化或基准记录的任务不能用来创建／确认新的设计计划，请新建任务")
    if state.get("plan", {}).get("approved"):
        raise ZeaError(ErrorCode.BAD_ARGS, "此任务的设计计划已确认，修改要求请新建任务")


def _query(spec: Spec, limit: int) -> list[dict[str, Any]]:
    return library.query(na=spec.na, magnification=spec.magnification, correction_class=spec.correction_class,
                         objective_field_number=spec.objective_field_number, working_distance_min=spec.item("working_distance").threshold,
                         tube_lens_mm=spec.tube_lens_focal_length, limit=limit)


def candidates(result: dict[str, Any]) -> list[dict[str, Any]]:
    return _query(parse_spec(result["spec"]), 5)


def starting_design(entry_id: str, result: dict[str, Any], rationale: str) -> dict[str, Any]:
    if not rationale.strip():
        interview.bad("选择初始结构时必须写 --rationale，说明选择理由和需调整之处")
    entry = library.load_entry(entry_id)
    if entry.get("status") != "admitted" or not entry.get("repro", {}).get("pass"):
        interview.bad("初始结构必须已经收录并通过复现校验")
    spec = parse_spec(result["spec"])
    match = next(r for r in _query(spec, len(library.all_entries())) if r["id"] == entry_id)
    notes = list(match["notes"])
    if match["scaled"]["optical_length_mm"] > spec.item("optical_length").threshold:
        notes.append("按焦距缩放的光学长度估计超过规格上限，需要优化；该估计尚未重新追迹。")
    if match["scaled"]["clear_diameter_mm"] > spec.item("clear_diameter").threshold:
        notes.append("按焦距缩放的通光直径估计超过规格上限，需要重新评估。")
    if entry["metrics"].get("cover_glass") != spec.cover_glass:
        notes.append("库中盖玻片条件与目标规格不同，需要重新适配并验收。")
    glass_changes = [g["material"] for g in entry["metrics"].get("glasses", [])
                     if g.get("catalog", "").upper() not in spec.glass_catalogs or g.get("status") not in spec.glass_allowed_status]
    if glass_changes:
        notes.append("需处理不符合目标目录或状态的玻璃：" + "、".join(glass_changes) + "。选择此结构不会自动放宽玻璃约束。")
    return {"entry": entry, "query": match, "rationale": rationale, "adjustments": notes,
            "prescription": library.read_json(library.entry_dir(entry_id) / "prescription.json")}


def create(job_id: str, draft_path: Path, entry_id: str | None, rationale: str = "") -> dict[str, Any]:
    from zea.plan_html import render

    job = jobs.get_job(job_id)
    _draft_job(job)
    draft = interview.read_draft(draft_path)
    result = interview.check(draft)
    if not result["ready"]:
        raise ZeaError(ErrorCode.BAD_ARGS, "需求访谈尚未完成，请先补充待问问题", {"questions": result["questions"]})
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
    html = render(payload, revision)
    (directory / "index.html").write_text(html, encoding="utf-8")
    artifacts = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in ("plan.json", "spec.json", "index.html")}
    write_json(directory / "artifacts.json", artifacts)
    pointer = {"revision": revision, "approved": False, "state": payload["state"]}
    write_json(job.root / "plan" / "current.json", pointer)
    job.update_state(plan=pointer)
    return {"job": job.id, "revision": revision, "state": payload["state"], "page": str(directory / "index.html"),
            "assumptions": sum(r["source"] == "assumption" for r in result["fields"])}


def inspect(job_id: str, revision: str | None = None) -> dict[str, Any]:
    job = jobs.get_job(job_id)
    pointer = read_json(job.root / "plan" / "current.json")
    revision = revision or pointer["revision"]
    if not isinstance(revision, str) or len(revision) != 64 or any(c not in "0123456789abcdef" for c in revision):
        interview.bad("设计计划版本必须为完整 SHA-256")
    if revision != pointer["revision"]:
        raise ZeaError(ErrorCode.SPEC_CHANGED, "这不是当前设计计划，请重新审阅最新版本")
    directory = job.root / "plan" / revision
    payload = read_json(directory / "plan.json")
    if digest(payload) != revision or payload.get("job") != job.id:
        raise ZeaError(ErrorCode.SPEC_CHANGED, "设计计划内容已变化，请重新生成并审阅")
    artifacts = read_json(directory / "artifacts.json")
    for name in ("plan.json", "spec.json", "index.html"):
        if not (directory / name).is_file() or hashlib.sha256((directory / name).read_bytes()).hexdigest() != artifacts.get(name):
            raise ZeaError(ErrorCode.SPEC_CHANGED, f"设计计划文件 {name} 已变化，请重新生成并审阅")
    if read_json(directory / "spec.json") != payload["review"]["spec"]:
        raise ZeaError(ErrorCode.SPEC_CHANGED, "计划展示的规格与待交接规格不一致")
    live_config = acceptance.acceptance_config(parse_spec(payload["review"]["spec"]))
    if live_config != payload["acceptance_config"]:
        raise ZeaError(ErrorCode.SPEC_CHANGED, "当前评估器口径与计划不一致，请重新生成并审阅")
    return {"job": job.id, "revision": revision, "state": pointer["state"], "approved": pointer["approved"],
            "page": str(directory / "index.html"), "payload": payload}


def confirm(job_id: str, revision: str, evidence: str) -> dict[str, Any]:
    if not evidence.strip():
        interview.bad("需要 --evidence 记录用户在对话中明确确认该版本的原话")
    reviewed = inspect(job_id, revision)
    job = jobs.get_job(job_id)
    if reviewed["approved"]:
        return {"job": job_id, "revision": revision, "state": "confirmed", "already_confirmed": True}
    _draft_job(job)
    if reviewed["payload"]["starting_design"] is None:
        interview.bad("尚未选定已收录的初始结构；先完成离线入库或选择已有结构，再生成计划")
    payload = reviewed["payload"]
    config = payload["acceptance_config"]
    spec_path = f"spec/plan-{revision}.json"
    write_json(job.root / spec_path, payload["review"]["spec"])
    approval = {"revision": revision, "approved": True, "state": "confirmed", "confirmed_at": jobs.now_iso(),
                "evidence": evidence, "spec": spec_path, "config_sha256": acceptance.config_hash(config)}
    write_json(job.root / "plan" / revision / "approval.json", approval)
    write_json(job.root / "plan" / "current.json", approval)
    job.update_state(spec=spec_path, plan=approval)
    return {"job": job.id, **approval, "next": "确认已记录；准备初始镜头后，按本规格进入第 0 轮验收。"}


def handoff(job_id: str) -> dict[str, Any]:
    """自动阶段读取确认记录时，再检查任务规格未被改动。"""
    reviewed = inspect(job_id)
    if not reviewed["approved"]:
        interview.bad("设计计划尚未得到用户确认")
    job = jobs.get_job(job_id)
    approval = read_json(job.root / "plan" / reviewed["revision"] / "approval.json")
    if job.read_state().get("spec") != approval["spec"] or read_json(job.root / approval["spec"]) != reviewed["payload"]["review"]["spec"]:
        raise ZeaError(ErrorCode.SPEC_CHANGED, "任务规格已偏离用户确认的设计计划")
    return {"job": job_id, "revision": reviewed["revision"], "spec": approval["spec"],
            "config_sha256": approval["config_sha256"], "starting_design": reviewed["payload"]["starting_design"],
            "note": "此处方是库中原结构快照，尚未按用户规格重新适配，不能把原库验收当作本任务验收。"}
