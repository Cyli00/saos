"""候选在同一需求、采样和预算下先初筛再短优化，记录结构、来源与未评估原因。"""

from __future__ import annotations

import json
import math
import shutil
import tempfile
import warnings
from pathlib import Path
from typing import Any

from saos import demand, geometry, i18n, jobs, lens_files, model_gate, prescription
from saos.protocol import ErrorCode, SaosError, jsonable, write_json

MIN_CANDIDATES = 1
MAX_CANDIDATES = 12
STAGE_KINDS = ("local", "hammer", "global")
CYCLES = ("auto", "1", "5", "10", "50")
# 候选允许的附加字段：写在候选里，原样进入 plan 与结果 / extra candidate fields, kept verbatim in the plan and the result
EXTRA_FIELDS = ("form", "provenance", "prediction", "constraints")
# 候选搭建后和优化后的结构描述，与 candidates/<id>/ 同目录 / the candidate's structure as built and at the end, in its folder
STRUCTURE_FILE = "structure.json"
# 有效或预算内无效均保留在比较中，计算与材料错误单列。
COUNTED = ("valid", "invalid")


def _bad(message: str) -> SaosError:
    return SaosError(ErrorCode.BAD_ARGS, f"scan plan: {message}")


def _accounted(row: dict[str, Any]) -> bool:
    """候选是否完成评估。"""
    return row.get("verdict") in COUNTED


def _built(row: dict[str, Any]) -> tuple[str, Any] | None:
    """候选搭建后的结构指纹和材料列表；没有记录时 None。 / The fingerprint and materials of the candidate as built; None when unrecorded."""
    as_built = (row.get("structure") or {}).get("as_built") or {}
    return (as_built["fingerprint"], as_built.get("materials")) if as_built.get("fingerprint") else None


def _same_structure(a: dict[str, Any], b: dict[str, Any]) -> bool:
    fa, fb = _built(a), _built(b)
    if fa is None or fb is None or fa[0] != fb[0]:
        return False
    return True


def counted(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """计入比较的候选：结论计入的，结构指纹相同只留一个（有效的优先留下）。
    The candidates that count: sound verdicts, one per structure fingerprint (a valid one kept in preference)."""
    out: list[dict[str, Any]] = []
    for r in sorted((r for r in rows if _accounted(r) and _built(r) is not None), key=lambda r: not r.get("valid")):
        if not any(_same_structure(k, r) for k in out):
            out.append(r)
    return out


def _scan_folders(job: jobs.Job) -> list[Path]:
    """scans/ 下的目录，最新的在前。/ The folders under scans/, newest first."""
    scans = job.root / "scans"
    return sorted((p for p in scans.iterdir() if p.is_dir()), reverse=True) if scans.is_dir() else []


def validate(root: Path, plan: dict[str, Any], items: dict[str, dict[str, Any]], measure: list[str]) -> dict[str, Any]:
    """校验 candidates、stages、prepare；返回补全后的计划（measure 加上全部硬条件）。
    Check candidates, stages and prepare; return the plan with every hard limit added to measure."""
    cands = plan["candidates"]
    if not isinstance(cands, list) or not MIN_CANDIDATES <= len(cands) <= MAX_CANDIDATES:
        raise _bad(f"candidates: {MIN_CANDIDATES}–{MAX_CANDIDATES} entries, each {{id, lens or prescription, optional hypothesis}}")
    seen = set()
    contents: dict[str, str] = {}
    for c in cands:
        if not isinstance(c, dict) or not demand.ID_RE.match(str(c.get("id", ""))) or c["id"] in seen:
            raise _bad("each candidate needs a unique id (letters, digits, _)")
        seen.add(c["id"])
        if (c.get("lens") is None) == (c.get("prescription") is None):
            raise _bad(f"{c['id']}: give either lens (a .zmx file) or prescription (a lens build JSON)")
        if c.get("lens") is not None:
            rel = str(c["lens"])
            path = jobs.inside(root, rel)
            if path.suffix.lower() != jobs.LENS_SUFFIX or not lens_files.exists(path):
                raise _bad(f"{c['id']}: lens must be a .zmx or Optiland snapshot in the job, as a job-relative path (got {rel!r})")
            # 比较的是结构：有快照时用快照的处方，否则用文件字节；名称、注释这些文件内容不影响比较
            # compare structures: the snapshot's prescription when there is one, else the file bytes; names and comments do not count
            payload = lens_files.read_companion(path)
            content = (json.dumps(payload["prescription"], sort_keys=True, ensure_ascii=False, default=str) if payload is not None
                       else path.read_bytes().hex())
        else:
            content = json.dumps(_read_prescription(jobs.inside(root, str(c["prescription"])), c["id"]), sort_keys=True, ensure_ascii=False,
                                 default=str)
        # 相同输入的重复运行不增加比较信息。
        twin = next((other for other, text in contents.items() if text == content), None)
        if twin is not None:
            raise _bad(f"{c['id']}: identical to {twin}; a candidate must differ in structure, not only in its name")
        contents[c["id"]] = content
    # stages 为空表示只做初筛：构建、有限追迹、一阶量和几何检查，不做任何优化 / empty stages: screening only, no optimisation
    stages = plan.get("stages", [])
    if not isinstance(stages, list):
        raise _bad(f'stages: a list such as [{{"local": {{"max_seconds": 60}}}}, {{"hammer": {{"max_seconds": 120}}}}], or [] to screen without optimising; kinds {STAGE_KINDS}')
    for s in stages:
        if not isinstance(s, dict) or len(s) != 1 or next(iter(s)) not in STAGE_KINDS:
            raise _bad(f"each stage is one of {STAGE_KINDS} with its settings")
        kind, spec = next(iter(s.items()))
        seconds = (spec or {}).get("max_seconds") if isinstance(spec, dict) else None
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
            raise _bad(f"{kind} stage: max_seconds must be a positive number; every stage needs a budget")
        if kind == "local" and str(spec.get("cycles", "auto")) not in CYCLES:
            raise _bad(f"local stage: cycles must be one of {CYCLES}")
        if kind == "local":
            from saos.backend.optiland_opt import ALGORITHMS, TORCH_ALGORITHMS, ZOS_ALGORITHMS

            if spec.get("algorithm") is not None and spec["algorithm"] not in ("auto", *ALGORITHMS, *TORCH_ALGORITHMS, *ZOS_ALGORITHMS):
                raise _bad("local stage: unknown algorithm")
            if spec.get("device") is not None and spec["device"] not in ("auto", "cpu", "cuda"):
                raise _bad("local stage: device must be auto, cpu or cuda")
    prepare = plan.get("prepare")
    if prepare is not None and not (isinstance(prepare, str) and jobs.inside(root, prepare).is_file()):
        raise _bad("prepare: a job-relative path of a script defining run(w)")
    if stages and not prepare and any(c.get("prescription") for c in cands):
        raise _bad("处方不带评价函数；含优化阶段的处方候选必须提供 prepare 脚本")
    for name in ("fixed", "solve", "compensate"):
        if plan.get(name) is not None:
            raise _bad(f"{name} does not apply to candidates (their surfaces differ); set things up in prepare")
    hard = [i for i, item in items.items() if item["kind"] in demand.HARD_KINDS]
    return {**plan, "measure": list(dict.fromkeys([*measure, *hard]))}


def _read_prescription(path: Path, cid: str) -> dict[str, Any]:
    """读出并校验一个候选的处方，错误带上候选名。/ Read and check a candidate's prescription; errors name the candidate."""
    if path.suffix.lower() != ".json" or not path.is_file():
        raise _bad(f"{cid}: prescription must be a .json file in the job, as a job-relative path")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise _bad(f"{cid}: prescription is not valid JSON: {exc}") from exc
    try:
        return prescription.normalize(data)
    except SaosError as exc:
        raise _bad(f"{cid}: {exc.message}") from exc


def budget_s(plan: dict[str, Any], workers: int) -> float:
    """各段预算之和乘以轮数（候选数 ÷ worker 数，向上取整），不含加载和测量。
    The stage budgets times the number of waves (candidates ÷ workers, rounded up), loading and measuring excluded."""
    per = sum(float(next(iter(s.values()))["max_seconds"]) for s in plan.get("stages", []))
    return per * math.ceil(len(plan["candidates"]) / max(1, workers))


def snapshot(root: Path, plan: dict[str, Any], folder: Path) -> list[dict[str, Any]]:
    """把每个候选连同同名的附属文件（处方只有它自己）复制到 scans/NNN/candidates/<id>/，之后改动原文件不影响比较。
    Copy each candidate with its same-stem companion files (a prescription alone) to scans/NNN/candidates/<id>/, so later
    edits do not reach it."""
    items = []
    for k, c in enumerate(plan["candidates"]):
        key = "lens" if c.get("lens") is not None else "prescription"
        src = jobs.inside(root, c[key])
        dest = folder / "candidates" / c["id"]
        dest.mkdir(parents=True)
        for p in src.parent.glob(src.stem + ".*") if key == "lens" else [src]:
            if p.is_file():
                shutil.copy2(p, dest / p.name)
        items.append({"i": k, "id": c["id"], "hypothesis": c.get("hypothesis", ""), "source": c[key],
                      key: (dest / src.name).relative_to(root).as_posix(), **{f: c[f] for f in EXTRA_FIELDS if f in c}})
    return items


def unchanged_since_start(job: jobs.Job, folder: Path) -> bool:
    """计划和候选副本在开始前已封存，之后没有改过（structure.json 是 worker 写的，不在其内）。
    The plan and the candidate copies were sealed before the run and are unchanged since (structure.json is written by the workers)."""
    from saos import stages

    plan_file = folder / "plan.json"
    if not stages.sealed(job.root, plan_file.relative_to(job.root).as_posix()):
        return False
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    paths = plan.get("input_files")
    files = ([jobs.inside(job.root, p) for p in paths] if paths is not None else
             [p for p in (folder / "candidates").rglob("*") if p.is_file() and p.name != STRUCTURE_FILE])
    files += list((folder / "candidates").glob(f"*/{STRUCTURE_FILE}"))
    return all(stages.sealed(job.root, p.relative_to(job.root).as_posix()) for p in files)


# ---------- worker 侧 / worker side ----------

def _state(w, plan: dict[str, Any]) -> dict[str, Any]:
    w.cache.clear()
    m = w.measure(plan["measure"])
    out = {k: m[k] for k in ("values", "shortfall", "gaps", "hard_failing", "invalid", "errors")}
    # 出错的类别（material 或 computation），判定时据此归类 / each failure's kind, used to file the verdict
    out["error_kinds"] = dict(m.get("error_kinds") or {})
    try:
        out["merit"] = float(w.backend.merit_value())
    except Exception:  # noqa: BLE001 - 没有评价函数时只是不记 / no merit function: just not recorded
        out["merit"] = None
    try:
        out["interference"] = geometry.summary(w.backend.interference())
    except Exception as exc:  # noqa: BLE001
        out["interference"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    return out


def _finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _problems(state: dict[str, Any]) -> list[str]:
    """候选无效的原因；空表示有效。G 为无穷大只因为有无效值时，原因已由 invalid 说明，不再重复。
    Why a candidate is invalid; empty when it is valid. An infinite G that only comes from invalid values is already
    explained by them."""
    out = []
    clash = state.get("interference") or {}
    if clash.get("error"):
        out.append(f"interference check failed: {clash['error']}")
    if clash.get("crossings") or clash.get("overlaps"):
        out.append("parts cross or interpenetrate")
    if state.get("hard_failing"):
        out.append(f"hard limits fail: {', '.join(state['hard_failing'])}")
    if state.get("invalid"):
        out.append(f"no valid value: {', '.join(state['invalid'])}")
    if state.get("errors"):
        out.append(f"checks failed: {', '.join(sorted(state['errors']))}")
    if not _finite(state.get("shortfall")) and not state.get("invalid"):
        out.append("no finite shortfall")
    return out


def _verdict(row: dict[str, Any], state: dict[str, Any]) -> str:
    """材料和计算错误先单列，其余分为有效或预算内无效。"""
    if "material" in (state.get("error_kinds") or {}).values():
        return "material"
    clash = state.get("interference") or {}
    if clash.get("error") or state.get("errors") or (not _finite(state.get("shortfall")) and not state.get("invalid")):
        return "computation"
    if not row.get("problems"):
        return "valid"
    return "invalid"


class _Numeric:
    """数过运行期间的 RuntimeWarning，只留第一条：逐光线的警告可能有很多。其他类别的警告原样交给 passthrough（原来的输出），
    不被吞掉。Counts the RuntimeWarnings of a run and keeps the first (per-ray warnings can be very many); every other
    warning goes unchanged to passthrough, the original output, so it is not swallowed."""

    def __init__(self, passthrough) -> None:
        self.count = 0
        self.first: str | None = None
        self.passthrough = passthrough

    def __call__(self, message, category, filename, lineno, file=None, line=None) -> None:
        if issubclass(category, RuntimeWarning):
            self.count += 1
            self.first = self.first or f"{Path(filename).name}:{lineno} {message}"[:300]
        else:
            self.passthrough(message, category, filename, lineno, file, line)


def _structure(desc: dict[str, Any]) -> dict[str, Any]:
    """结构指纹和材料列表：比较去重和选择都用它。/ The structure fingerprint and materials: used by duplicate removal and by selection."""
    return {"fingerprint": model_gate.digest(model_gate.fingerprint(desc)), "materials": jsonable(model_gate.materials(desc))}


def _saved(desc: dict[str, Any]) -> dict[str, Any]:
    """存进结构文件的描述：去掉文件路径和布局图，与 lens_state 的口径一致。 / The description as saved: without file path and layout, as lens_state."""
    return jsonable({k: v for k, v in desc.items() if k not in ("file", "layout")})


def _obsolete(w) -> list[dict[str, Any]] | None:
    b = w.backend
    try:
        catalogs = b._catalogs_in_use()
        out = []
        for s in b.surfaces():
            name = str(s.get("material") or "")
            if name and name.upper() != "MIRROR" and b.glass_status(name, catalogs) == "Obsolete":
                out.append({"surface": s["index"], "material": name})
        return out
    except Exception:  # noqa: BLE001 - 读不到状态时不报，不挡比较 / unreadable status: not reported, does not block
        return None


def _run_stage(w, kind: str, spec: dict[str, Any]) -> dict[str, Any]:
    cores = int(w.args.get("cores") or 1)
    if kind == "local":
        options = {key: spec[key] for key in ("algorithm", "device") if spec.get(key) is not None}
        if "device" in options and w.backend.identity().get("backend") != "optiland":
            raise _bad("local stage: device applies only to the Optiland backend")
        res = w.backend.optimize(str(spec.get("cycles", "auto")), cores, max_seconds=float(spec["max_seconds"]),
                                 should_cancel=w.cancel_requested, **options)
    else:
        res = w.backend.optimize_search(kind, cores, max_seconds=float(spec["max_seconds"]), should_cancel=w.cancel_requested)
    return {k: res.get(k) for k in ("initial_merit", "final_merit", "variables", "stopped_by", "elapsed_s", "materials_changed",
                                  "algorithm", "requested_algorithm", "device", "fallback", "improved", "converged", "succeeded",
                                  "accepted_steps", "rejected_steps", "restored", "admissible_result") if k in res}


def _evaluate(w, item: dict[str, Any], row: dict[str, Any], temp: Path) -> None:
    """加载副本或按处方搭建、记下搭建后的结构、prepare、起点、逐段优化与实测、终点检查、存镜头，定出结论。
    Load a copy or build the prescription, record the structure as built, prepare, record the start, optimise and measure
    stage by stage, check the end, save the lens, and set the verdict."""
    from saos.scan import _script_run

    plan = w.args["plan"]
    if item.get("prescription"):
        w.backend.build(_read_prescription(w.path(item["prescription"]), item["id"]))
        # 存成文件：Hammer 和全局搜索在镜头旁写中间文件，reload 也要它 / saved: Hammer and global search write next to it
        w.base = temp / f"{item['id']}{jobs.LENS_SUFFIX}"
        w.backend.save_as(w.base)
    else:
        src = w.path(item["lens"])
        for p in src.parent.iterdir():
            if p.is_file():
                shutil.copy2(p, temp / p.name)
        w.backend.load(temp / src.name)
        w.base = temp / src.name
    # 结构描述放在候选自己的目录里，与结果一起封存 / the structure goes in the candidate's own folder, sealed with the result
    folder = w.path(item.get("prescription") or item["lens"]).parent
    as_built = w.backend.describe_model(None)
    row["structure"] = {"as_built": _structure(as_built)}
    if plan.get("prepare"):
        try:
            row["prepare"] = jsonable(_script_run(w, plan["prepare"])(w))
        except Exception as exc:
            from saos.backend.optiland_materials import MaterialError

            if isinstance(exc, MaterialError) or (isinstance(exc, SaosError) and exc.code in (ErrorCode.ZOS_CONNECTION_LOST, ErrorCode.UI_BLOCKED)):
                raise
            raise SaosError(ErrorCode.BAD_ARGS, f"prepare {plan['prepare']} 执行失败：{type(exc).__name__}: {exc}",
                            {"candidate_setup": True, "phase": "prepare", "cause_code": getattr(exc, "code", None)}) from exc
    try:
        merit_rows = len(w.backend.merit_rows())
    except Exception:  # noqa: BLE001
        merit_rows = None
    if (plan.get("stages") or plan.get("require_merit")) and not merit_rows:
        raise SaosError(ErrorCode.BAD_ARGS, "候选没有评价函数；prepare 必须设置变量和评价函数", {"candidate_setup": True})
    row["setup"] = {"source": "prepare" if plan.get("prepare") else "candidate", "merit_rows": merit_rows}
    if w.backend.identity().get("backend") == "optiland":
        from saos.starts.prediction import predict

        try:
            row["prediction"] = predict(w.backend)
        except Exception as exc:
            row["prediction"] = {"errors": {"prediction": f"{type(exc).__name__}: {exc}"}}
    start = _state(w, plan)
    row["start"] = start
    stages = []
    for stage in plan["stages"]:
        if w.cancel_requested():
            row["cancelled"] = True
            break
        kind, spec = next(iter(stage.items()))
        run = _run_stage(w, kind, spec)
        stages.append({"kind": kind, **run, **_state(w, plan)})
    row["stages"] = stages
    if stages and stages[0].get("variables") is not None:
        row["setup"]["variables"] = stages[0]["variables"]
    final = stages[-1] if stages else start
    row.update({k: final[k] for k in ("values", "shortfall", "hard_failing", "invalid", "errors", "merit", "interference")})
    row["start_shortfall"] = start["shortfall"]
    row["problems"] = _problems(final)
    row["valid"] = not row["problems"]
    before = (stages[-2] if len(stages) > 1 else start)["shortfall"]
    after = final["shortfall"]
    rel = float(plan.get("rel", 0.01))
    # 最后一段还在明显下降：短预算低估了它，排名靠后也保留 / still falling in the last stage: the short budget undersells it
    row["still_improving"] = bool(stages) and math.isfinite(after) and (
        not math.isfinite(before) or before - after > rel * max(abs(before), 1e-12))
    row["obsolete"] = _obsolete(w)
    if w.args.get("lens_dir"):
        rel_lens = f"{w.args['lens_dir']}/{item['id']}{jobs.LENS_SUFFIX}"
        target = w.path(f"lenses/{rel_lens}")
        target.parent.mkdir(parents=True, exist_ok=True)
        w.backend.save_as(target)
        row["lens"] = rel_lens
    as_end = w.backend.describe_model(None)
    row["structure"]["end"] = _structure(as_end)
    write_json(folder / STRUCTURE_FILE, {"as_built": _saved(as_built), "end": _saved(as_end)})
    w.job.seal(folder / STRUCTURE_FILE)
    row["verdict"] = _verdict(row, final)
    row["valid"] = row["verdict"] == "valid"


def _failed(row: dict[str, Any], exc: Exception) -> None:
    """区分候选配置、材料与计算失败，配置错误交给扫描入口返回。"""
    from saos.backend.optiland_materials import MaterialError

    row["error"] = f"{type(exc).__name__}: {exc}"[:300]
    row["valid"] = False
    if isinstance(exc, SaosError) and exc.details.get("candidate_setup"):
        row["setup_error"] = exc.message
        row["error_kind"] = "configuration"
        row["setup_details"] = exc.details
        return
    row["verdict"] = "material" if isinstance(exc, MaterialError) else "computation"


def candidate(w, item: dict[str, Any]) -> dict[str, Any]:
    """一个候选：加载副本或按处方搭建、记下结构、prepare、起点、逐段优化与实测、终点检查、存镜头，定出结论。
    运行期间的数值 RuntimeWarning 一并记下；有效的候选仍有效，其余的记为 computation。
    One candidate: load a copy or build the prescription, record the structure, prepare, record the start, optimise and
    measure stage by stage, check the end, save the lens, and set the verdict. Numeric RuntimeWarnings of the run are
    recorded: a valid candidate stays valid, any other becomes computation."""
    row: dict[str, Any] = {k: item[k] for k in ("i", "id", "hypothesis", "source", *EXTRA_FIELDS) if k in item}
    temp = Path(tempfile.mkdtemp(prefix="saos-candidate-"))
    # 原来的输出函数要在替换前取到 / the original output function is taken before it is replaced
    numeric = _Numeric(warnings.showwarning)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("always")
            warnings.showwarning = numeric
            try:
                _evaluate(w, item, row, temp)
            except Exception as exc:  # noqa: BLE001 - 一个候选的失败记为结论，不让 worker 退出；连接和界面阻塞交给后台进程
                if isinstance(exc, SaosError) and exc.code in (ErrorCode.ZOS_CONNECTION_LOST, ErrorCode.UI_BLOCKED):
                    raise
                _failed(row, exc)
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    if numeric.count:
        row["numeric_warnings"] = {"count": numeric.count, "first": numeric.first}
        if row.get("verdict") == "invalid":
            row["verdict"] = "computation"
    row["numeric_warning_scope"] = "python_filters"
    return row


# ---------- 后台进程侧 / daemon side ----------

def rank(rows: list[dict[str, Any]], keep: int) -> tuple[list[dict[str, Any]], list[str]]:
    """先有效后无效，各自按目标差距；保留前 keep 个有效的和所有预算受限的。返回 (排序后的行, 保留的 id)。
    Valid before invalid, each by shortfall; keep the first `keep` valid ones and every budget-limited one."""
    def key(r: dict[str, Any]):
        g = r.get("shortfall")
        return (0 if r.get("valid") else 1, g if isinstance(g, (int, float)) and math.isfinite(g) else math.inf, r["i"])

    for r in rows:
        # 没跑完的候选只有回退行，补上名字 / a candidate that did not finish has only a fallback row
        r.setdefault("id", str(r["i"]))
    ordered = sorted(rows, key=key)
    kept = [r["id"] for r in ordered if r.get("valid")][:max(0, keep)]
    kept += [r["id"] for r in ordered if r.get("still_improving") and r["id"] not in kept]
    for r in ordered:
        r["kept"] = r["id"] in kept
    return ordered, kept


def _num(x: Any) -> str:
    from saos.report import fmt

    return fmt(x) if isinstance(x, (int, float)) and math.isfinite(x) else "—"


def page(job: jobs.Job, folder: Path, plan: dict[str, Any], rows: list[dict[str, Any]], lang: str | None) -> Path:
    """给用户看的对比页 scans/NNN/index.html。/ The comparison page for the user, scans/NNN/index.html."""
    from saos.report import CSS, e, header_row

    chosen = i18n.resolve(None, lang)

    def t(zh: str, en: str) -> str:
        return i18n.text(chosen, zh, en)

    stages = " → ".join(f"{next(iter(s))} {_num(next(iter(s.values()))['max_seconds'])} s" for s in plan["stages"])
    labels = {"valid": t("有效", "valid"), "invalid": t("预算内无效", "invalid within budget"), "material": t("未评估：材料", "not evaluated: material"),
              "computation": t("未评估：计算", "not evaluated: computation")}
    counted_ids = {r["id"] for r in counted(rows)}
    body = []
    not_evaluated = []
    for r in rows:
        verdict = labels.get(r.get("verdict"), t("未知", "unknown")) + (t("，计入", ", counted") if r["id"] in counted_ids else "")
        verdict += " · " + (t("保留", "kept") if r.get("kept") else t("淘汰", "dropped")) + (t("（预算受限）", " (budget-limited)") if r.get("still_improving") else "")
        if r.get("numeric_warnings"):
            nw = r["numeric_warnings"]
            verdict += f"<br><span class=\"muted\">{e(t('数值警告', 'numeric warnings'))} {nw['count']}: {e(nw['first'])}</span>"
        problems = "; ".join(r.get("problems") or ([] if r.get("valid") else [r.get("error") or t("未运行完", "did not finish")]))
        merits = " → ".join(_num(s.get("merit")) for s in [r.get("start") or {}, *(r.get("stages") or [])])
        clash = r.get("interference") or {}
        thin = clash.get("thinnest_glass") or {}
        gap = clash.get("smallest_air_gap") or {}
        setup = r.get("setup") or {}
        obsolete = ", ".join(f"{o['surface']}:{o['material']}" for o in r.get("obsolete") or []) or "—"
        verdict += " · " + t("优化后仍失败" if r.get("stages") and not r.get("valid") else "已做短优化" if r.get("stages") else "仅初筛",
                               "still failing after optimisation" if r.get("stages") and not r.get("valid") else "short optimisation" if r.get("stages") else "screening only")
        body.append(
            f"<tr><td>{e(r['id'])}</td><td>{e(r.get('hypothesis', ''))}</td>"
            f"<td>{e(verdict)}<br><span class=\"muted\">{e(problems)}</span></td>"
            f"<td class=\"num\">{_num(r.get('start_shortfall'))} → {_num(r.get('shortfall'))}</td><td class=\"num\">{merits}</td>"
            f"<td class=\"num\">{_num(thin.get('min'))} / {_num(gap.get('min'))}</td><td>{e(obsolete)}</td>"
            f"<td>{e(str(setup.get('variables', '—')))} / {e(str(setup.get('merit_rows', '—')))} · {e(setup.get('source', '—'))}</td>"
            f"<td>{e(r.get('lens') or '—')}</td></tr>")
        if r.get("verdict") in ("material", "computation"):
            not_evaluated.append(f"<tr><td>{e(r['id'])}</td><td>{e(labels[r['verdict']])}</td>"
                                 f"<td>{e(r.get('error') or '; '.join(sorted((r.get('errors') or {}).values())) or '—')}</td></tr>")
    unevaluated_html = ""
    if not_evaluated:
        unevaluated_html = (f"<h2>{t('未评估', 'Not evaluated')}</h2><p class=\"muted\">{t('材料或计算出错的候选不计入比较，它们没有判断结构的依据。',
                                                                     'Candidates with a material or computation failure do not count: they give no ground to judge the structure.')}</p>"
                            f"<div class=\"wrap\"><table><thead><tr>{header_row(t('候选|结论|原因', 'Candidate|Verdict|Reason'))}</tr></thead>"
                            f"<tbody>{''.join(not_evaluated)}</tbody></table></div>")
    ids = plan["measure"]
    values = "".join(f"<tr><td>{e(r['id'])}</td>" + "".join(f"<td class=\"num\">{_num((r.get('values') or {}).get(i))}</td>" for i in ids) + "</tr>"
                     for r in rows)
    html = f"""<!doctype html><html lang="{chosen}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{t('结构探索', 'Structure exploration')} · {e(job.id)}</title><style>{CSS}</style></head><body><main>
<p class="muted">{e(job.id)} · {e(folder.relative_to(job.root).as_posix())} · {e(jobs.now_iso())}</p>
<h1>{t('结构探索', 'Structure exploration')}</h1>
<p class="muted">{t('短优化失败只说明预算内没有找到解。数值警告计数受检查脚本的 Python 警告过滤器影响，零条警告不证明计算有效。',
                      'Failure on a short budget does not prove impossibility. Python warning filters in check scripts can suppress numeric warnings; a zero count does not establish validity.')}</p>
<p>{t('每个候选在同样的预算下优化，用需求单的判定比较：', 'Each candidate was optimised on the same budget and judged by the demand: ')}{e(stages)}{t('。', '. ')}
{t(f'计入比较的结构 {len(counted_ids)} 个（结构指纹相同的只算一个）。排序先看是否有效，再看目标差距 G。短预算的排名只说明这份预算下谁更好；最后一段 G 仍明显下降的标为预算受限，排名靠后也保留。',
   f'{len(counted_ids)} structures count (one per structure fingerprint). Valid candidates rank first, then by shortfall G. A short-budget ranking only says who did better on this budget; a candidate whose G still fell clearly in its last stage is marked budget-limited and kept however it ranks.')}</p>
<div class="wrap"><table><thead><tr>{header_row(t('候选|说明|结论|G 起点 → 终点|merit 起点 → 各段|最薄玻璃 / 最小空气间隔|停产玻璃|变量数 / 评价函数行数 · 来源|镜头',
                                              'Candidate|Notes|Verdict|G start → end|merit start → stages|Thinnest glass / smallest air gap|Obsolete glass|Variables / merit rows · source|Lens'))}</tr></thead>
<tbody>{''.join(body)}</tbody></table></div>
{unevaluated_html}
<p class="muted">{t('“来源”为 prepare 时所有候选的变量和评价函数由同一个脚本设定；为 candidate 时用候选文件自带的，变量数和评价函数行数不同的候选之间比较不完全公平。',
                    'Source "prepare" means one script set the variables and merit function of every candidate; "candidate" means each file brought its own, so candidates with different variable or merit-row counts are not compared on equal terms.')}</p>
<h2>{t('终点各项实测值', 'Measured values at the end')}</h2>
<div class="wrap"><table><thead><tr>{header_row(t('候选', 'Candidate') + '|' + '|'.join(ids))}</tr></thead><tbody>{values}</tbody></table></div>
</main></body></html>"""
    out = folder / "index.html"
    out.write_text(html, encoding="utf-8")
    return out
