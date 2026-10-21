"""结构探索（saos scan 的 candidates 计划）：几个结构不同的起始方案，在同样的预算、同样的判定下各优化一段再比较。
起型、选材、片数、组合错了，后面的优化很难调回来，所以先比较几个结构，只把胜出的带进建模确认和深度优化。
Structure exploration (the candidates plan of saos scan): structurally different starting designs, each optimised for
the same budget and judged by the same checks, then compared. A wrong starting form, glass choice, element count or
grouping is hard to optimise away, so several structures are compared first and only the winners go on.

    {"candidates": [{"id": "triplet", "lens": "explore/start/triplet.zmx", "hypothesis": "…"},
                    {"id": "petzval", "prescription": "explore/start/petzval.json", "hypothesis": "…"}, …],
     "prepare": "explore/start/prepare.py",           # 可选：每个候选上执行 run(w)，统一设变量和评价函数 / optional run(w) on each
     "stages": [{"local": {"max_seconds": 60}}, {"hammer": {"max_seconds": 120}}],   # 也可以 {"global": {…}}
     "measure": ["spot", "ttl"], "keep": 3, "workers": 4}

候选给镜头文件（lens）或处方（prescription，lens build 的格式，在 worker 里搭建，改片数、材料只要改 JSON）。
每个候选一个 worker：在临时目录里加载候选副本或按处方搭建（Hammer、全局搜索在镜头旁写的中间文件互不干扰），执行 prepare，记下起点，
逐段优化并在每段后按需求单实测，结束时做交叠检查和玻璃状态检查。硬条件总在所测项里。排序先看是否有效（零件不交叠、
硬条件满足、判定没出错），再看目标差距；最后一段目标差距仍下降超过 rel 的标为"预算受限"，排名靠后也保留。
A candidate is a lens file (lens) or a prescription (prescription, the lens build format, built in the worker, so a
different element count or glass is a JSON edit). One worker per candidate: a copy of the candidate is loaded, or the
prescription built, in a temp folder (so the files Hammer and global search
write next to the lens never collide), prepare runs, the start is recorded, each stage optimises and is measured with
the demand's checks, and the end gets the interference and glass-status checks. Hard limits are always measured.
Ranking puts valid candidates (no clashing parts, hard limits kept, checks ran) first, then sorts by shortfall; a
candidate whose last stage still cut the shortfall by more than rel is marked budget-limited and kept however it ranks.
"""

from __future__ import annotations

import json
import math
import shutil
import tempfile
from pathlib import Path
from typing import Any

from saos import demand, geometry, i18n, jobs, lens_files, prescription
from saos.protocol import ErrorCode, SaosError, jsonable

MIN_CANDIDATES = 2
MAX_CANDIDATES = 12
# 结构未指定的任务，进入深度优化前至少要有这么多有效候选的比较；两个入围方案的再比较仍允许（计划 §4.3）
MIN_STRUCTURE_CANDIDATES = 3
STAGE_KINDS = ("local", "hammer", "global")
CYCLES = ("auto", "1", "5", "10", "50")


def _bad(message: str) -> SaosError:
    return SaosError(ErrorCode.BAD_ARGS, f"scan plan: {message}")


# 只有这些原因算“物理上不成立”，可作为淘汰证据；计算失败、解析错误和未运行都不算已评估
# only these reasons count as physically impossible, i.e. as evidence to eliminate; failed, unparsed or unrun is not an evaluation
PHYSICAL_PROBLEMS = frozenset({"parts cross or interpenetrate"})


def _accounted(row: dict[str, Any]) -> bool:
    """候选是否已有依据的结论：有效，或被物理矛盾淘汰。 / Whether a candidate has a sound verdict: valid, or eliminated physically."""
    problems = row.get("problems") or []
    return bool(row.get("valid")) or (bool(problems) and set(problems) <= PHYSICAL_PROBLEMS)


def structure_compared(job: jobs.Job, demand_sha: str, method_sha: str) -> bool:
    """本需求版本、本判定版本下是否有一份已封存的结构比较：计划和结果都封存，至少 MIN_STRUCTURE_CANDIDATES 个候选各有
    依据的结论（有效，或物理矛盾淘汰），且其中至少一个有效。计算失败的候选不计入。两个入围方案的复比不需要再凑第三个。
    改了判定脚本（checks）后比较失效：比较的数值是用旧判定算的。
    Whether a sealed comparison exists under this demand and check version: at least MIN_STRUCTURE_CANDIDATES candidates
    with a sound verdict (valid, or eliminated by a physical contradiction), at least one of them valid. Failed candidates
    do not count. Re-comparing two finalists needs no third. Changed checks void it."""
    from saos import stages

    scans = job.root / "scans"
    for plan_path in sorted(scans.glob("*/plan.json")) if scans.is_dir() else []:
        folder = plan_path.parent
        result_path = folder / "result.json"
        if not result_path.is_file():
            continue
        rel_plan, rel_result = plan_path.relative_to(job.root).as_posix(), result_path.relative_to(job.root).as_posix()
        if not (stages.sealed(job.root, rel_plan) and stages.sealed(job.root, rel_result)):
            continue
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if plan.get("mode") != "candidates" or result.get("mode") != "candidates":
            continue
        if plan.get("demand_sha") != demand_sha or plan.get("method_sha") != method_sha:
            continue
        rows = result.get("rows") or []
        accounted = [r for r in rows if _accounted(r)]
        if len(accounted) >= MIN_STRUCTURE_CANDIDATES and any(r.get("valid") for r in accounted):
            return True
    return False


def validate(root: Path, plan: dict[str, Any], items: dict[str, dict[str, Any]], measure: list[str]) -> dict[str, Any]:
    """校验 candidates、stages、prepare；返回补全后的计划（measure 加上全部硬条件）。
    Check candidates, stages and prepare; return the plan with every hard limit added to measure."""
    cands = plan["candidates"]
    if not isinstance(cands, list) or not MIN_CANDIDATES <= len(cands) <= MAX_CANDIDATES:
        raise _bad(f"candidates: {MIN_CANDIDATES}–{MAX_CANDIDATES} entries, each {{id, lens or prescription, hypothesis}}")
    seen = set()
    contents: dict[str, str] = {}
    for c in cands:
        if not isinstance(c, dict) or not demand.ID_RE.match(str(c.get("id", ""))) or c["id"] in seen:
            raise _bad("each candidate needs a unique id (letters, digits, _)")
        seen.add(c["id"])
        if not str(c.get("hypothesis", "")).strip():
            raise _bad(f"{c['id']}: write the hypothesis (why this structure might win) before comparing")
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
        # 内容完全相同的候选只是同一个结构换了名字；它们不能凑够“三个结构”（更细的结构差异仍由假设和比较证据说明）
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
    prepare = plan.get("prepare")
    if prepare is not None and not (isinstance(prepare, str) and jobs.inside(root, prepare).is_file()):
        raise _bad("prepare: a job-relative path of a script defining run(w)")
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
        items.append({"i": k, "id": c["id"], "hypothesis": c["hypothesis"], "source": c[key],
                      key: (dest / src.name).relative_to(root).as_posix()})
    return items


# ---------- worker 侧 / worker side ----------

def _state(w, plan: dict[str, Any]) -> dict[str, Any]:
    w.cache.clear()
    m = w.measure(plan["measure"])
    out = {k: m[k] for k in ("values", "shortfall", "hard_failing", "invalid", "errors")}
    try:
        out["merit"] = float(w.backend.merit_value())
    except Exception:  # noqa: BLE001 - 没有评价函数时只是不记 / no merit function: just not recorded
        out["merit"] = None
    try:
        out["interference"] = geometry.summary(w.backend.interference())
    except Exception as exc:  # noqa: BLE001
        out["interference"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    return out


def _problems(state: dict[str, Any]) -> list[str]:
    """候选无效的原因；空表示有效。/ Why a candidate is invalid; empty when it is valid."""
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
    if not (isinstance(state.get("shortfall"), (int, float)) and math.isfinite(state["shortfall"])):
        out.append("no finite shortfall")
    return out


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
        res = w.backend.optimize(str(spec.get("cycles", "auto")), cores, max_seconds=float(spec["max_seconds"]),
                                 should_cancel=w.cancel_requested)
    else:
        res = w.backend.optimize_search(kind, cores, max_seconds=float(spec["max_seconds"]), should_cancel=w.cancel_requested)
    return {k: res.get(k) for k in ("initial_merit", "final_merit", "variables", "stopped_by", "elapsed_s", "materials_changed") if k in res}


def candidate(w, item: dict[str, Any]) -> dict[str, Any]:
    """一个候选：加载副本或按处方搭建、prepare、起点、逐段优化与实测、终点检查、存镜头。
    One candidate: load a copy or build the prescription, prepare, record the start, optimise and measure stage by stage,
    check the end, save the lens."""
    from saos.scan import _script_run

    plan = w.args["plan"]
    row: dict[str, Any] = {k: item[k] for k in ("i", "id", "hypothesis", "source")}
    temp = Path(tempfile.mkdtemp(prefix="saos-candidate-"))
    try:
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
        if plan.get("prepare"):
            row["prepare"] = jsonable(_script_run(w, plan["prepare"])(w))
        try:
            merit_rows = len(w.backend.merit_rows())
        except Exception:  # noqa: BLE001
            merit_rows = None
        row["setup"] = {"source": "prepare" if plan.get("prepare") else "candidate", "merit_rows": merit_rows}
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
        return row
    finally:
        shutil.rmtree(temp, ignore_errors=True)


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
    body = []
    for r in rows:
        verdict = (t("保留", "kept") if r.get("kept") else t("淘汰", "dropped")) + (t("（预算受限）", " (budget-limited)") if r.get("still_improving") else "")
        problems = "; ".join(r.get("problems") or ([] if r.get("valid") else [r.get("error") or t("未运行完", "did not finish")]))
        merits = " → ".join(_num(s.get("merit")) for s in [r.get("start") or {}, *(r.get("stages") or [])])
        clash = r.get("interference") or {}
        thin = clash.get("thinnest_glass") or {}
        gap = clash.get("smallest_air_gap") or {}
        setup = r.get("setup") or {}
        obsolete = ", ".join(f"{o['surface']}:{o['material']}" for o in r.get("obsolete") or []) or "—"
        body.append(
            f"<tr><td>{e(r['id'])}</td><td>{e(r.get('hypothesis', ''))}</td><td>{e(verdict)}<br><span class=\"muted\">{e(problems)}</span></td>"
            f"<td class=\"num\">{_num(r.get('start_shortfall'))} → {_num(r.get('shortfall'))}</td><td class=\"num\">{merits}</td>"
            f"<td class=\"num\">{_num(thin.get('min'))} / {_num(gap.get('min'))}</td><td>{e(obsolete)}</td>"
            f"<td>{e(str(setup.get('variables', '—')))} / {e(str(setup.get('merit_rows', '—')))} · {e(setup.get('source', '—'))}</td>"
            f"<td>{e(r.get('lens') or '—')}</td></tr>")
    ids = plan["measure"]
    values = "".join(f"<tr><td>{e(r['id'])}</td>" + "".join(f"<td class=\"num\">{_num((r.get('values') or {}).get(i))}</td>" for i in ids) + "</tr>"
                     for r in rows)
    html = f"""<!doctype html><html lang="{chosen}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{t('结构探索', 'Structure exploration')} · {e(job.id)}</title><style>{CSS}</style></head><body><main>
<p class="muted">{e(job.id)} · {e(folder.relative_to(job.root).as_posix())} · {e(jobs.now_iso())}</p>
<h1>{t('结构探索', 'Structure exploration')}</h1>
<p>{t('每个候选在同样的预算下优化，用需求单的判定比较：', 'Each candidate was optimised on the same budget and judged by the demand: ')}{e(stages)}{t('。', '. ')}
{t('排序先看是否有效（零件不交叠、硬条件满足、判定没出错），再看目标差距 G。短预算的排名只说明这份预算下谁更好；最后一段 G 仍明显下降的标为预算受限，排名靠后也保留。',
   'Valid candidates (no clashing parts, hard limits kept, checks ran) rank first, then by shortfall G. A short-budget ranking only says who did better on this budget; a candidate whose G still fell clearly in its last stage is marked budget-limited and kept however it ranks.')}</p>
<div class="wrap"><table><thead><tr>{header_row(t('候选|假设|结论|G 起点 → 终点|merit 起点 → 各段|最薄玻璃 / 最小空气间隔|停产玻璃|变量数 / 评价函数行数 · 来源|镜头',
                                              'Candidate|Hypothesis|Verdict|G start → end|merit start → stages|Thinnest glass / smallest air gap|Obsolete glass|Variables / merit rows · source|Lens'))}</tr></thead>
<tbody>{''.join(body)}</tbody></table></div>
<p class="muted">{t('“来源”为 prepare 时所有候选的变量和评价函数由同一个脚本设定；为 candidate 时用候选文件自带的，变量数和评价函数行数不同的候选之间比较不完全公平。',
                    'Source "prepare" means one script set the variables and merit function of every candidate; "candidate" means each file brought its own, so candidates with different variable or merit-row counts are not compared on equal terms.')}</p>
<h2>{t('终点各项实测值', 'Measured values at the end')}</h2>
<div class="wrap"><table><thead><tr>{header_row(t('候选', 'Candidate') + '|' + '|'.join(ids))}</tr></thead><tbody>{values}</tbody></table></div>
</main></body></html>"""
    out = folder / "index.html"
    out.write_text(html, encoding="utf-8")
    return out
