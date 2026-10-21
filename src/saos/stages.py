"""按任务封存 OpticStudio 阶段入口，隔离预算和比较窗口。"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from saos import demand, jobs, ledger, lens_files, model_gate
from saos.protocol import ErrorCode, SaosError, write_json

ENTRY = "stages/zos.json"
RESULT = "stages/zos-result.json"
REPORT = "report/latest.json"


def read(root: Path) -> dict[str, Any] | None:
    path = root / ENTRY
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def backend(root: Path) -> str:
    """任务现在应使用的计算后端：认可入口（zos begin）之后是 zos，之前是 optiland。
    不由 .zmx 文件、伴随文件或环境变量决定；daemon、worker 和判定都只调用这里。"""
    return "zos" if read(root) is not None else "optiland"


def rounds(root: Path, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entry = read(root)
    return [r for r in history if r.get("stage") == "zos" and int(r["round"]) >= entry["first_round"]] if entry else history


def stop(root: Path, policy: dict[str, Any]) -> dict[str, Any]:
    entry = read(root)
    return {**policy, "max_rounds": entry["max_rounds"]} if entry else policy


def require_ready(root: Path) -> None:
    if read(root) and not completed(root):
        raise SaosError(ErrorCode.BAD_ARGS, "the OpticStudio entry measurement is incomplete; resume saos zos begin with the recorded quote")


def sealed(root: Path, relative: str) -> bool:
    path, recorded = root / jobs.SEALS_FILE, None
    if not path.is_file() or not (root / relative).is_file():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if row.get("path") == relative:
                recorded = row.get("sha256")
    return recorded is not None and recorded == jobs.sha256_file(root / relative)


def completed(root: Path) -> bool:
    return sealed(root, ENTRY) and sealed(root, RESULT)


def measurement_evidence(backend, entry):
    from saos.backend.optiland_materials import MaterialStore

    current_materials, raw_changes = MaterialStore(), []
    for key, saved in entry["material_data"].items():
        try:
            catalogs, value = json.loads(key)
            current = current_materials.get(value, catalogs).to_dict()
            if current != saved:
                raw_changes.append({"material": value, "catalogs": catalogs, "saved": saved, "current": current})
        except (SaosError, ValueError, TypeError) as exc:
            raw_changes.append({"key": key, "unavailable": str(exc)})
    return {"baseline": _baseline(backend), "material_data": raw_changes}


def differences(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for key in sorted(set(before) | set(after)):
        a, b = before.get(key), after.get(key)
        try:
            x, y = float(a), float(b)
            delta = y - x if math.isfinite(x) and math.isfinite(y) else None
            relative = delta / abs(x) if delta is not None and x != 0 else (0.0 if delta == 0 else None)
        except (ValueError, TypeError):
            delta = relative = None
        rows.append({"item": key, "optiland": a, "zos": b, "delta": delta, "relative": relative})
    return rows


def _baseline(backend) -> dict[str, Any]:
    from saos.backend.baseline import baseline

    try:
        return baseline(backend)
    except Exception as exc:
        return {"errors": {"baseline": f"{type(exc).__name__}: {exc}"}}


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        return {k: v for name, item in value.items() if name != "errors"
                for k, v in _flatten(item, f"{prefix}.{name}" if prefix else name).items()}
    if isinstance(value, list):
        return {k: v for i, item in enumerate(value) for k, v in _flatten(item, f"{prefix}[{i}]").items()}
    return {prefix: value}


def begin(ctx, quote: str, selected: int | None = None, max_rounds: int | None = None) -> dict[str, Any]:
    from saos import workflow
    from saos.backend.optiland import OptilandBackend

    job, root = ctx.job, ctx.job.root
    quote = quote.strip()
    if not quote:
        raise SaosError(ErrorCode.BAD_ARGS, "--quote must contain the user's own words accepting the report")
    if max_rounds is not None and (isinstance(max_rounds, bool) or max_rounds < 1):
        raise SaosError(ErrorCode.BAD_ARGS, "--max-rounds must be a positive integer")
    h, sha, confirmation = workflow.preflight(root)
    scripts = demand.prepare_scripts(root, h)
    method_sha = demand.method_digest(scripts)
    problems = job.verify_seals()
    if problems:
        raise SaosError(ErrorCode.EVIDENCE_TAMPERED, "sealed evidence changed; restore it before entering OpticStudio", {"problems": problems[:50]})
    history = ledger.load(root / ledger.LEDGER_FILE, h)
    entry = read(root)
    if entry:
        if (entry["quote"] != quote or selected not in (None, entry["source_round"])
                or max_rounds not in (None, entry["max_rounds"])):
            raise SaosError(ErrorCode.BAD_ARGS, "this job already has an immutable OpticStudio entry; resume with its original arguments")
        if completed(root):
            return {"entry": entry, **json.loads((root / RESULT).read_text(encoding="utf-8")), "resumed": True}
        if (demand.as_current(entry["demand_sha"], h, "compute", root), entry["method_sha"]) != (sha, method_sha):
            raise SaosError(ErrorCode.SPEC_CHANGED, "restore the demand and checks of the pending OpticStudio entry before resuming")
    else:
        meta_path = root / REPORT
        if not meta_path.is_file():
            raise SaosError(ErrorCode.BAD_ARGS, "generate a current saos report and show it to the user before zos begin")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        archive = (root / str(meta.get("archived", ""))).resolve()
        if archive.parent != (root / "report").resolve() or not archive.with_suffix(".json").is_file():
            raise SaosError(ErrorCode.BAD_ARGS, "the report metadata has no archived evidence; generate a new report")
        if meta != json.loads(archive.with_suffix(".json").read_text(encoding="utf-8")):
            raise SaosError(ErrorCode.EVIDENCE_TAMPERED, "latest report metadata differs from its sealed archive")
        if not sealed(root, meta["archived"]) or not sealed(root, archive.with_suffix(".json").relative_to(root).as_posix()):
            raise SaosError(ErrorCode.EVIDENCE_TAMPERED, "the report and its metadata must both be sealed")
        shown = root / "report" / "index.html"
        if not shown.is_file() or shown.read_bytes() != archive.read_bytes():
            raise SaosError(ErrorCode.EVIDENCE_TAMPERED, "the displayed report differs from the archived report; regenerate it before asking for acceptance")
        if ((demand.as_current(meta.get("demand_sha"), h, "compute", root), meta.get("method_sha")) != (sha, method_sha)
                or meta.get("last_round") != (history[-1]["round"] if history else None)):
            raise SaosError(ErrorCode.SPEC_CHANGED, "the report is stale; regenerate it and ask the user to accept that report")
        selected = meta["round"] if selected is None else selected
        eligible = ledger.current_window(history)
        source = next((r for r in eligible if r["round"] == selected and ledger.version(r) == (sha, method_sha)), None)
        if source is None or not ledger.deliverable(source) or not math.isfinite(source["shortfall"]):
            raise SaosError(ErrorCode.BAD_ARGS, "the selected round must be deliverable under the current demand and checks")
        payload = lens_files.read_companion(job.resolve_lens(source["file"], must_exist=True))
        if payload is None:
            raise SaosError(ErrorCode.BAD_ARGS, "zos begin requires an Optiland round with its prescription companion")
        evaluation = json.loads(jobs.inside(root, source["evaluation"]).read_text(encoding="utf-8"))
        if (evaluation.get("environment") or {}).get("backend") != "optiland":
            raise SaosError(ErrorCode.BAD_ARGS, "the accepted round must have been measured in Optiland")
        if lens_files.exists(job.resolve_lens("zos-entry.zmx", must_exist=False)):
            raise SaosError(ErrorCode.FILE_EXISTS, "zos-entry.zmx already exists; rename that working lens before zos begin")
        native = OptilandBackend()
        try:
            native.start()
            native.load(job.resolve_lens(source["file"], must_exist=True))
            desc = native.describe_model(None)
            approved = model_gate.require_approved(job, desc)
            if model_gate.model_glass_surfaces(desc) or source.get("model_glass_approximation"):
                raise SaosError(ErrorCode.BAD_ARGS, "replace every Optiland model-glass approximation with a real material before zos begin")
            reference = _baseline(native)
        finally:
            native.close()
        entry = {"stage": "zos", "quote": quote, "source_round": selected, "source_file": source["file"],
                 "source_values": source.get("values") or {}, "source_materials": source.get("materials") or [],
                 "source_baseline": reference, "material_data": payload.get("materials") or {},
                 "report": meta["archived"], "optiland_conclusion": meta["status"],
                 "demand_sha": sha, "method_sha": method_sha, "confirmation": confirmation, "model": approved["model"],
                 "max_rounds": max_rounds if max_rounds is not None else h["stop"]["max_rounds"],
                 "first_round": len(history), "time": jobs.now_iso()}

    existing = next((r for r in history if r.get("stage") == "zos" and r.get("verify_of") == entry["source_round"]), None)
    captured = None
    if existing is None:
        # 启动 OpticStudio 并加载入口镜头；拿不到许可证时不写入口，任务仍停在 Optiland。
        source_path = job.resolve_lens(entry["source_file"], must_exist=True)
        ctx._daemon.loaded = None
        ctx.backend, captured = ctx._daemon.enter_zos(job, source_path)
        backend = ctx.backend
        desc = backend.describe_model(None)
        model_gate.require_approved(job, desc)
        working = "zos-entry.zmx"
        if not read(root):
            (root / "stages").mkdir(exist_ok=True)
            entry["untranslated"] = backend.system_info().get("untranslated") or []
            write_json(root / ENTRY, entry)
        if not sealed(root, ENTRY):
            job.seal(root / ENTRY)
        ctx.save_as(working, overwrite=True)
        measured = workflow.record_round(ctx, note=f"OpticStudio measurement of r{entry['source_round']:03d}",
                                         verify_of=entry["source_round"])
        existing = measured["record"]
    else:
        # ledger 已写入而比较文件尚未完成时，只补比较文件，不重复记录复测轮。
        if not sealed(root, ENTRY):
            job.seal(root / ENTRY)

    evidence = existing.get("verification_evidence") or {
        "baseline": {"errors": {"baseline": "this older verify round did not archive baseline measurements"}},
        "material_data": [{"unavailable": "this older verify round did not archive current material data"}]}
    measured_baseline = evidence["baseline"]
    material_rows = {str((m.get("surface"), m.get("configuration"))): m for m in existing.get("materials") or []}
    old_materials = {str((m.get("surface"), m.get("configuration"))): m for m in entry["source_materials"]}
    material_diff = [{"location": k, "optiland": old_materials.get(k), "zos": material_rows.get(k)}
                     for k in sorted(set(old_materials) | set(material_rows)) if old_materials.get(k) != material_rows.get(k)]
    comparison = {"round": existing["round"], "verify_of": entry["source_round"],
                  "items": differences(entry["source_values"], existing.get("values") or {}),
                  "baseline": differences(_flatten(entry["source_baseline"]), _flatten(measured_baseline)),
                  "baseline_errors": {"optiland": entry["source_baseline"].get("errors"), "zos": measured_baseline.get("errors")},
                  "materials": material_diff, "material_data": evidence["material_data"],
                  "untranslated": entry.get("untranslated") or [],
                  "evaluation": existing["evaluation"], "time": jobs.now_iso()}
    write_json(root / RESULT, comparison)
    job.seal(root / RESULT)
    # 进入前窗口里的未保存内容，若有，以副本路径返回（不进入封存的比较）
    return {"entry": entry, **comparison, **({"window_captured": captured} if captured else {})}
