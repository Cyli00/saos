"""并发扫描与多起点寻优（saos scan）：不写脚本，按计划在 worker 池里逐点改参数、可选求解一个变量、补偿、按需求单实测。
Concurrent scans and multi-start minimisation (saos scan): no script; per point the plan sets parameters, optionally
solves one variable, compensates and measures with the demand's checks, on the daemon's worker pool.

    saos scan --job J --plan plan.json

plan.json（vars、points、minimize、probes、candidates、tolerance 六选一 / exactly one of vars, points, minimize, probes, candidates, tolerance）：
    {"vars": {"lens1 back.curvature": [0, 1, 2, 3], "3.conic": {"from": -1, "to": 0, "steps": 5}},  # 网格 / grid
     "points": [{"5.radius": 2.1, "5.conic": -0.5}, …],                                              # 或逐点 / or a list
     "minimize": {"vars": {"5.radius": {"step": 0.01, "min": 1.5, "max": 3}, "12.par1": {"step": 0.03}},
                  "starts": "current" | [{…}, …], "method": "nelder-mead" | "powell",
                  "max_evals": 200, "max_seconds": 900},                                            # 每个起点一个 worker / one worker per start
     "fixed": {"tube outer wall.thickness": -0.015},          # 每个点都先写入 / written before every point
     "solve": {"var": "lens dome.curvature", "lo": 0, "hi": 8, "target": {"item": "working_distance"}, "value": 15},
              # 或 {"operand": ["EFFL", 0, 1]}；每个点在 [lo, hi] 内找 target = value（先扫 scan 个点再 Brent）
              # or {"operand": [...]}; per point find target = value in [lo, hi] (scan `scan` points, then Brent)
     "compensate": null | "quick_focus" | {"script": "explore/refocus.py"},   # run(w)
     "measure": ["spot", "fov"],               # 默认需求单所有带界限的项 / default every bounded item
     "operands": {"efl": ["EFFL", 0, 1]},      # 另外报告的数 / extra numbers to report
     "workers": 6, "keep": 3, "note": "…"}

扰动诊断（probes）：在开始时的镜头上对 1–6 个变量各取 −delta、+delta，加上不扰动的基准共 2N+1 个点，补偿后实测，
再与 agent 事先写下的预测对照（方向、阴性对照、补偿是否起作用），按对目标的归一化效应排序：
Perturbation probes: each of 1–6 variables at −delta and +delta from the start lens, plus the unperturbed base (2N+1
points), compensated and measured, then compared with the predictions written beforehand and ranked by the normalised
effect on the target:
    {"target": "spot", "mechanism": "Petzval 场曲 …", "compensate": "quick_focus",
     "probes": [{"id": "l1_rear_r", "key": "3.radius", "delta": 0.02, "prediction": "…", "expect": {"spot": "down"}},
                {"id": "l2_asph", "script": "explore/l2_a4.py", "delta": 1e-3, "control": true,   # run(w, delta)
                 "prediction": "只改像散分配，不改 Petzval 和", "expect": {"spot": "none"}}]}

公差（tolerance）：按公差表扰动当前镜头，用需求单的判定逐点实测。先做灵敏度（每项单独取下限、上限），再做蒙特卡洛（各项同时随机），
给出每个判定项的分布、达标率和全部达标的比例（良率）。序列、非序列同一套；补偿（如重新调焦）用 compensate：
Tolerance: perturb the current lens by a tolerance table and measure each point with the demand's checks: sensitivity
(each item alone at its lower and upper limit), then Monte Carlo (all items drawn at once), giving each check's
distribution, pass rate and the share of samples meeting every goal (yield):
    {"tolerance": {"items": [{"key": "3.thickness", "min": -0.05, "max": 0.05, "source": "厂家公差表 …"},
                             {"key": "4.nd", "min": -0.001, "max": 0.001, "dist": "normal", "source": "…"},
                             {"key": "object.lens.tilt_x", "min": -0.1, "max": 0.1, "source": "…"},
                             {"key": "2.radius", "min": -0.002, "max": 0.002, "relative": true, "source": "…"}],
                   "samples": 200, "seed": 1, "sensitivity": true},
     "compensate": "quick_focus", "workers": 6}
min、max 是相对标称值的偏差（relative 时为比例）；dist 为 uniform（默认）或 normal（σ = (max−min)/4，截在 [min, max]）。

结构探索（candidates）：几个结构不同的起始镜头在同样的预算、同样的判定下各优化一段再比较，格式和规则见 saos.candidates。
Structure exploration (candidates): structurally different start lenses, each optimised on the same budget and judged by
the same checks, then compared; format and rules in saos.candidates.

结果在 scans/NNN/（计划和 result.json，封存）；按目标差距（只含所测各项）排序，最好的 keep 个点的镜头存到
lenses/scans/NNN/pPPP.zmx，可以直接 saos lens load。当前镜头不变。minimize 与 opt local 一样要求需求已确认、没有 blocking 问题。
Results go to scans/NNN/ (the plan and result.json, sealed), ranked by shortfall over the measured items; the lenses of
the best `keep` points are saved as lenses/scans/NNN/pPPP.zmx, ready for saos lens load. The current lens is unchanged.
minimize, like opt local, needs a confirmed demand without blocking questions.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import shutil
import threading
from typing import Any

from saos import candidates, demand, jobs, kit, search
from saos.protocol import ErrorCode, SaosError, write_json

log = logging.getLogger(__name__)

SCANS_DIR = "scans"
MODES = ("vars", "points", "minimize", "probes", "candidates", "tolerance")
MAX_TOLERANCES = 200
TOL_DISTS = ("uniform", "normal")
MAX_POINTS = 5000
HELP_EPILOG = """Input: --plan JSON (choose exactly one of the six modes)
  Parameter keys use a surface index (negative counts from image; -1 is image)
  or a unique Lens Data comment: "3.radius", "lens dome.curvature", "12.par1".
  Surface parameters: radius, curvature (1/R, 0 for flat), thickness, conic,
  semi_diameter, mech_semi_diameter, material, comment, par<N>, nd, vd (writing
  nd or vd turns the material into a model glass; workers reload every point).
  System keys: aperture, field.<n>.x/y, wave.<n>, config.<configuration>.<row>.
  NSC keys: object.<number or comment>.<parameter>, e.g. object.Cortex.vp.g;
  parameters: x/y/z, tilt_x/tilt_y/tilt_z, material, comment, par<N>, column
  header, mean_path, vp.<DLL parameter>, nd, vd, face<N>.is|coating|fraction|
  sigma|rays. Mixed mode: object.<object>@<NonSequential surface>.<parameter>.
  File-based objects are referenced by number or file name, never by comment.
  Details: uv run saos exec --help.

  Grid (vars): lists or inclusive ranges; at most 5000 points.
    {"vars": {"3.radius": [20, 25, 30],
              "5.conic": {"from": -1, "to": 0, "steps": 5}},
     "measure": ["spot"], "workers": 4, "keep": 3}

  Explicit points (1 to 5000 nonempty parameter dictionaries):
    {"points": [{"3.radius": 20}, {"3.radius": 25}], "measure": ["spot"]}

  Structure candidates (2 to 12; workflow requires at least 3 different structures
  unless the user prescribed the structure):
    {"candidates": [
       {"id": "triplet", "lens": "explore/start/triplet.zmx",
        "hypothesis": "Fewer elements may meet the target"},
       {"id": "petzval", "prescription": "explore/start/petzval.json",
        "hypothesis": "Separate field flattening may improve the edge"},
       {"id": "double_gauss", "lens": "explore/start/double_gauss.zmx",
        "hypothesis": "Symmetry may reduce off-axis aberrations"}],
     "prepare": "explore/start/prepare.py",
     "stages": [{"local": {"max_seconds": 60, "cycles": "auto"}},
                {"hammer": {"max_seconds": 120}},
                {"global": {"max_seconds": 120}}],
     "measure": ["spot"], "workers": 3, "keep": 3}
    Each candidate needs unique id, hypothesis and exactly one job-relative lens
    (.zmx) or prescription (.json). Format: uv run saos lens build --help.
    Every stage requires a positive max_seconds; local cycles are auto, 1, 5,
    10 or 50. Optional prepare defines run(w), setting variables/merit for each
    candidate. fixed/solve/compensate do not apply; use prepare instead.
    No current lens is needed. Each worker builds/loads a private candidate copy,
    runs prepare, records the start and every stage, then checks interference
    and glass status. All hard conditions are always measured. Valid candidates
    rank first, then by shortfall. A last-stage relative gain > demand.stop.rel
    means still_improving (budget-limited), so keep it regardless of rank.
    Default keep: 3 valid candidates plus all budget-limited candidates.
    plan.json records budget_s: sum of stage budgets times ceil(candidates/workers),
    excluding loading/measurement; detached response also reports it.

  Multi-start minimization (one search per start):
    {"minimize": {"vars": {"3.radius": {"step": 0.1, "min": 15, "max": 40},
                           "5.conic": {"step": 0.02}},
                  "starts": [{"3.radius": 20}, {"3.radius": 30}],
                  "method": "nelder-mead", "max_evals": 200, "max_seconds": 900},
     "measure": ["spot"], "workers": 2}
    starts defaults to "current"; method is nelder-mead or powell. Each variable
    needs a nonzero step; bounds min/max are optional. The objective is the same
    shortfall definition as the demand, over measured items, not a new merit.
    minimize and candidates require a confirmed demand without blocking questions.

  Perturbation probes (1 to 6; base plus +/-delta = 2N+1 points):
    {"target": "spot", "mechanism": "Petzval field curvature limits the edge",
     "compensate": "quick_focus",
     "probes": [
       {"id": "rear_r", "key": "3.radius", "delta": 0.02,
        "prediction": "Less power should lower edge spot by a few micrometres",
        "expect": {"spot": "down"}},
       {"id": "control", "script": "explore/control.py", "delta": 0.001,
        "control": true, "prediction": "This changes only astigmatism",
        "expect": {"spot": "none"}}]}
    target is a demand check id; write mechanism and each prediction beforehand.
    Each probe needs unique id, nonzero finite delta and exactly one numeric key
    or job-relative script defining run(w, delta). Probe flat faces by curvature.
    expect maps measured ids to up/down/none/valley/peak. No confirmation needed.
    ranking reports normalized effects and one_sided when only one side is valid;
    valley/peak means the baseline is already at an extremum in that variable.
    review flags a missing/moving control, failed prediction, no variable moving
    the target, or ineffective focus compensation. An invalid baseline target
    must be fixed before interpreting the probes.

  Tolerance (sensitivity and Monte Carlo on the current lens):
    {"tolerance": {"items": [
       {"key": "3.thickness", "min": -0.05, "max": 0.05, "source": "maker's table"},
       {"key": "4.nd", "min": -0.001, "max": 0.001, "dist": "normal", "source": "..."},
       {"key": "object.lens.tilt_x", "min": -0.1, "max": 0.1, "source": "..."},
       {"key": "2.radius", "min": -0.002, "max": 0.002, "relative": true,
        "source": "..."}],
     "samples": 200, "seed": 1, "sensitivity": true},
     "compensate": "quick_focus", "workers": 6}
    1 to 200 items, each a unique numeric parameter key (thickness, radius or
    curvature, par<N>, <surface>.nd/.vd, object.<object>.x|tilt_x|...; not
    material or comment) with deviations min < max from nominal (fractions
    when relative) and a required source; optional id. dist is uniform
    (default) or normal (sigma = (max - min)/4, truncated to [min, max]).
    samples is 0 to 5000; sensitivity defaults to true; at least one of them.
    Points: nominal, sensitivity (each item alone at min and at max) and
    samples Monte Carlo draws (all items at once; fixed seed repeats). With
    nd/vd items an extra model-glass nominal is added: writing nd/vd swaps the
    catalog glass for a model glass whose dispersion differs slightly even at
    zero deviation (Cooke sample: EFL +0.0005 mm), so those sensitivities use it
    as baseline and the swap itself is model_glass_offset (included in samples).
    Every point is compensated, then measured with the demand's checks. Result
    adds nominal (values, failing items), sensitivity (both sides, change and
    effect normalized by the demand scale, sorted by largest; worst names the
    most affected check) and monte_carlo (per check n, mean, std, min, max,
    5/50/95% percentiles, pass rate; yield_hard: all hard bounds met; yield_all:
    every measured item met). Requires a confirmed demand without blocking
    questions. uv run saos report includes the latest tolerance scan.

  Common optional fields (except candidates' restrictions above):
    {"fixed": {"5.thickness": 2},
     "solve": {"var": "3.curvature", "lo": 0, "hi": 0.1,
               "target": {"item": "working_distance"}, "value": 15, "scan": 9},
     "compensate": {"script": "explore/refocus.py"},
     "measure": ["spot"], "operands": {"efl": ["EFFL", 0, 1]},
     "workers": 4, "keep": 3, "note": "Purpose of this scan"}
    Order: fixed, point values/probe script, solve, compensate, demand checks.
    solve.target can instead be {"operand": ["EFFL", 0, 1]}; value defaults to 0,
    scan to 9; optional xtol. Scan for a sign change then Brent root finding.
    compensate is null, "quick_focus" or {"script": "job-relative.py"} (run(w)).
    measure defaults to bounded/directional demand items; probes add target.
    keep defaults to 3 (probes and tolerance: 0). --workers overrides plan
    workers, default 4, limited by point count and available cores; count the
    8-instance licence, including user windows and daemon. Cold worker startup
    is about 6-8 s each.

Results and files
  Non-candidate modes require a loaded, saved lens of this job; it stays unchanged.
  scans/NNN/ seals plan.json, base.zmx (or candidates/<id>/ copies), result.json
  and all rows. The response summarizes the top 10 rows ranked by shortfall.
  Kept point lenses: lenses/scans/NNN/pPPP.zmx, ready for uv run saos lens load.
  Candidate lenses use <id>.zmx; results add kept, budget_limited, best and page
  (scans/NNN/index.html: hypotheses, validity, stages, measurements, interference,
  glass status and variable/merit source, prepare or candidate).
  --detach uses a background worker-pool thread, not runs/; the daemon remains
  available. Read scans/NNN/progress.json via uv run saos scan --job J --show [NNN]
  (default latest). The pool serves one scan or ctx.map at a time.
  uv run saos job cancel J stops after workers finish their current points.

Common errors
  BAD_ARGS, NO_LENS_LOADED, JOB_MISMATCH, UNSAVED_CHANGES, DEMAND_NOT_CONFIRMED,
  DEMAND_BLOCKED, POOL_BUSY (wait or cancel), ZOS_ERROR (no usable workers).
"""
# valley / peak：±delta 两侧都比基准差（或都好）超过 NONE_BELOW，基准正在这个变量的极值上，一阶斜率看不出
# valley / peak: both sides above (or below) the base by more than NONE_BELOW; the base sits at an extremum the slope cannot show
DIRECTIONS = ("up", "down", "none", "valley", "peak")
MAX_PROBES = 6
# 归一化变化（Δ/scale）小于这个值算"没有变化" / a normalised change below this counts as "none"
NONE_BELOW = 0.05


def _bad(message: str) -> SaosError:
    return SaosError(ErrorCode.BAD_ARGS, f"scan plan: {message}")


def _keys(where: str, values: Any) -> None:
    if not isinstance(values, dict) or not values:
        raise _bad(f"{where} must map parameter keys to values")
    for key in values:
        kit.parse_key(key)


def _keep(plan: dict[str, Any], default: int) -> int:
    keep = plan.get("keep", default)
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 0:
        raise _bad(f"keep must be an integer >= 0, got {keep!r}")
    return keep


def validate(plan: Any, d: dict[str, Any], root=None) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise _bad("must be a JSON object")
    modes = [m for m in MODES if m in plan]
    if len(modes) != 1:
        raise _bad(f"give exactly one of {MODES}")
    mode = modes[0]
    items = {i["id"]: i for i in demand.checked(d)}
    measure = plan.get("measure") or [i for i, item in items.items() if item["goal"] is not None or "direction" in item or i == plan.get("target")]
    unknown = [m for m in measure if m not in items]
    if unknown:
        raise _bad(f"measure has ids that are not checks of the demand: {unknown}")
    if mode == "vars":
        _keys("vars", plan["vars"])
        count = math.prod(len(search.axis(v)) for v in plan["vars"].values())
        if count > MAX_POINTS:
            raise _bad(f"the grid has {count} points; at most {MAX_POINTS} per scan")
    elif mode == "points":
        if not isinstance(plan["points"], list) or not plan["points"] or len(plan["points"]) > MAX_POINTS:
            raise _bad(f"points must be a list of 1–{MAX_POINTS} parameter dicts")
        for p in plan["points"]:
            _keys("each point", p)
    elif mode == "probes":
        measure = _validate_probes(plan, items, measure)
    elif mode == "tolerance":
        _validate_tolerance(plan)
    elif mode == "candidates":
        return {**candidates.validate(root, plan, items, measure), "mode": mode, "keep": _keep(plan, 3),
                "rel": float(d["stop"]["rel"]), "lang": d.get("lang")}
    else:
        m = plan["minimize"]
        if not isinstance(m, dict) or not isinstance(m.get("vars"), dict) or not m["vars"]:
            raise _bad('minimize needs "vars": {key: {"step": s, "min"?: a, "max"?: b}}')
        for key, spec in m["vars"].items():
            kit.parse_key(key)
            if not isinstance(spec, dict) or not isinstance(spec.get("step"), (int, float)) or not spec["step"]:
                raise _bad(f"minimize var {key} needs a non-zero step")
        starts = m.get("starts", "current")
        if starts != "current":
            if not isinstance(starts, list) or not starts:
                raise _bad('minimize starts: "current" or a list of {key: value}')
            for s in starts:
                _keys("each start", s)
        if m.get("method", "nelder-mead") not in ("nelder-mead", "powell"):
            raise _bad("minimize method: nelder-mead or powell")
    if plan.get("fixed"):
        _keys("fixed", plan["fixed"])
    solve = plan.get("solve")
    if solve is not None:
        if not isinstance(solve, dict) or not {"var", "lo", "hi", "target"} <= set(solve):
            raise _bad('solve needs var, lo, hi, target ({"item": id} or {"operand": [type, int1, int2, data…]}) and value')
        kit.parse_key(solve["var"])
        target = solve["target"]
        if not (isinstance(target, dict) and (target.get("item") in items or isinstance(target.get("operand"), list))):
            raise _bad("solve target: {\"item\": <check id of the demand>} or {\"operand\": [type, int1, int2, data…]}")
    comp = plan.get("compensate")
    if not (comp is None or comp == "quick_focus" or (isinstance(comp, dict) and set(comp) == {"script"})):
        raise _bad('compensate must be null, "quick_focus" or {"script": "<path in the job>"}')
    operands = plan.get("operands") or {}
    if not isinstance(operands, dict) or any(not isinstance(v, list) or not v for v in operands.values()):
        raise _bad("operands map a name to [type, int1, int2, data…]")
    return {**plan, "mode": mode, "measure": measure, "keep": _keep(plan, 0 if mode in ("probes", "tolerance") else 3)}


def _validate_tolerance(plan: dict[str, Any]) -> None:
    tol = plan["tolerance"]
    if not isinstance(tol, dict):
        raise _bad('tolerance must be {"items": [...], "samples": N, "seed": S, "sensitivity": true}')
    extra = sorted(set(tol) - {"items", "samples", "seed", "sensitivity"})
    if extra:
        raise _bad(f"tolerance has unknown keys {extra}")
    entries = tol.get("items")
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_TOLERANCES:
        raise _bad(f"tolerance.items: 1 to {MAX_TOLERANCES} entries")
    seen = set()
    for k, it in enumerate(entries):
        where = f"tolerance.items[{k}]"
        if not isinstance(it, dict):
            raise _bad(f"{where} must be a JSON object")
        extra = sorted(set(it) - {"key", "min", "max", "dist", "relative", "source", "id"})
        if extra:
            raise _bad(f"{where} has unknown keys {extra}")
        key = it.get("key")
        if not isinstance(key, str):
            raise _bad(f"{where}.key: a numeric lens parameter such as 3.thickness, 4.nd or object.lens.tilt_x")
        if kit.parse_key(key)[2] in ("material", "comment"):
            raise _bad(f"{where}.key must be a numeric parameter (perturb a glass with <surface>.nd and .vd)")
        if key in seen:
            raise _bad(f"{where}.key {key} appears twice")
        seen.add(key)
        lo, hi = it.get("min"), it.get("max")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (lo, hi)) or lo > hi or lo == hi:
            raise _bad(f"{where}: min and max are finite deviations from the nominal value with min < max")
        if it.get("dist", "uniform") not in TOL_DISTS:
            raise _bad(f"{where}.dist must be one of {TOL_DISTS}")
        if not isinstance(it.get("relative", False), bool):
            raise _bad(f"{where}.relative must be true or false")
        if not str(it.get("source", "")).strip():
            raise _bad(f"{where}.source: say where the tolerance comes from (a maker's table, the user, a standard)")
    samples = tol.get("samples", 0)
    if isinstance(samples, bool) or not isinstance(samples, int) or not 0 <= samples <= MAX_POINTS:
        raise _bad(f"tolerance.samples must be an integer 0–{MAX_POINTS}")
    if not isinstance(tol.get("sensitivity", True), bool):
        raise _bad("tolerance.sensitivity must be true or false")
    if not samples and not tol.get("sensitivity", True):
        raise _bad("tolerance: ask for sensitivity, samples or both")


def _tolerance_items(ctx, plan: dict[str, Any]) -> list[dict[str, Any]]:
    """标称点、灵敏度点（每项单独取 min、max）和蒙特卡洛样本（各项同时抽取，种子固定可复现）。
    The nominal point, the sensitivity points (each item alone at min and max) and the Monte Carlo samples (one seed)."""
    tol = plan["tolerance"]
    entries = tol["items"]
    nominal = kit.values(ctx, [it["key"] for it in entries])
    bad = [k for k, v in nominal.items() if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)]
    if bad:
        raise _bad(f"tolerance: {bad} have no finite current value (a flat surface's radius: tolerance its curvature)")

    def value(it: dict[str, Any], dev: float) -> float:
        base = float(nominal[it["key"]])
        return base * (1.0 + dev) if it.get("relative") else base + dev

    out: list[dict[str, Any]] = [{"i": 0, "set": {}, "probe": "nominal"}]
    glass = [it["key"] for it in entries if _is_glass_key(it["key"])]
    if glass:
        # 写 nd、vd 会把目录玻璃换成模型玻璃，偏差为 0 时色散也略有不同（Cooke 样例实测 EFL 偏 +0.0005 mm）；
        # 这些项的灵敏度相对“换成模型玻璃的标称点”算，换玻璃本身的偏移另报
        # writing nd or vd turns a catalog glass into a model glass whose dispersion differs a little even at zero deviation
        # (+0.0005 mm EFL on the Cooke sample); their sensitivity is read against the model-glass nominal and the offset reported
        out.append({"i": 1, "set": {k: float(nominal[k]) for k in glass}, "probe": "nominal_model_glass"})
    if tol.get("sensitivity", True):
        for it in entries:
            for side in ("min", "max"):
                if it[side] != 0:
                    out.append({"i": len(out), "set": {it["key"]: value(it, float(it[side]))}, "probe": it.get("id", it["key"]),
                                "side": side, "deviation": float(it[side])})
    rng = random.Random(tol.get("seed", 1))

    def draw(it: dict[str, Any]) -> float:
        lo, hi = float(it["min"]), float(it["max"])
        if it.get("dist", "uniform") == "uniform":
            return rng.uniform(lo, hi)
        center, sigma = (lo + hi) / 2, (hi - lo) / 4
        while True:
            x = rng.gauss(center, sigma)
            if lo <= x <= hi:
                return x

    for s in range(int(tol.get("samples", 0))):
        devs = {it["key"]: draw(it) for it in entries}
        out.append({"i": len(out), "set": {it["key"]: value(it, devs[it["key"]]) for it in entries}, "sample": s,
                    "deviations": devs})
    return out


def _is_glass_key(key: str) -> bool:
    return kit.parse_key(key)[2] in ("nd", "vd")


def _percentile(values: list[float], q: float) -> float:
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _tolerance_result(plan: dict[str, Any], checked: dict[str, dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """灵敏度表（每项对每个判定值的改变，按对目标的归一化影响排序）和蒙特卡洛统计（分布、各项达标率、良率）。
    The sensitivity table (each item's change of every checked value, ranked by normalised effect) and the Monte Carlo
    statistics (distribution, pass rate per check, yield)."""
    measure = plan["measure"]
    nominal = next((r for r in rows if r.get("probe") == "nominal"), {})
    base = nominal.get("values") or {}
    model_row = next((r for r in rows if r.get("probe") == "nominal_model_glass"), None)
    model_base = (model_row or {}).get("values") or {}

    def scale(k: str) -> float:
        item = checked[k]
        if item.get("goal") is None:
            return abs(_finite(base.get(k)) or 0.0) or 1.0
        return float(item.get("scale", demand._default_scale(item["goal"]) or 1.0))

    def passes(k: str, v: Any) -> bool | None:
        goal = checked[k].get("goal")
        if goal is None:
            return None
        v = _finite(v)
        return v is not None and demand.excess(goal, v) <= demand.BOUND_TOLERANCE * scale(k)

    sens: dict[str, dict[str, Any]] = {}
    for r in rows:
        if "side" not in r:
            continue
        key = next(iter(r["set"]))
        entry = sens.setdefault(r["probe"], {"item": r["probe"], "key": key, "sides": {}})
        values = r.get("values") or {}
        ref = model_base if _is_glass_key(key) and model_row is not None else base
        if ref is model_base:
            entry["baseline"] = "nominal_model_glass"
        change = {k: (None if _finite(values.get(k)) is None or _finite(ref.get(k)) is None else values[k] - ref[k]) for k in measure}
        entry["sides"][r["side"]] = {"deviation": r["deviation"], "values": values, "change": change,
                                     "failing": [k for k in measure if passes(k, values.get(k)) is False]}
    for entry in sens.values():
        effects = {k: max((abs(s["change"][k]) / scale(k) for s in entry["sides"].values() if s["change"].get(k) is not None), default=None)
                   for k in measure}
        entry["normalised_effect"] = effects
        entry["worst"] = max(((k, v) for k, v in effects.items() if v is not None), key=lambda kv: kv[1], default=(None, None))[0]
    ranking = sorted(sens.values(), key=lambda e: -max((v for v in e["normalised_effect"].values() if v is not None), default=0.0))
    samples = [r for r in rows if "sample" in r]
    mc: dict[str, Any] = {"samples": len(samples)}
    if samples:
        valid = [r for r in samples if not r.get("invalid") and not r.get("error")]
        stats = {}
        for k in measure:
            vals = [v for v in (_finite((r.get("values") or {}).get(k)) for r in valid) if v is not None]
            flags = [passes(k, (r.get("values") or {}).get(k)) for r in samples]
            stats[k] = {"n": len(vals), "nominal": _finite(base.get(k)),
                        "mean": sum(vals) / len(vals) if vals else None,
                        "std": math.sqrt(sum((v - sum(vals) / len(vals)) ** 2 for v in vals) / (len(vals) - 1)) if len(vals) > 1 else None,
                        "min": min(vals) if vals else None, "max": max(vals) if vals else None,
                        **({f"p{int(q * 100)}": _percentile(vals, q) for q in (0.05, 0.5, 0.95)} if vals else {}),
                        "pass_rate": None if flags and flags[0] is None else sum(1 for f in flags if f) / len(samples)}
        mc.update(stats=stats, invalid=len(samples) - len(valid),
                  # 良率：硬界限全部满足的样本比例，以及所测各项全部达标的样本比例 / yield: hard bounds met, and every measured goal met
                  yield_hard=sum(1 for r in samples if not r.get("invalid") and not r.get("error") and not r.get("hard_failing")) / len(samples),
                  yield_all=sum(1 for r in samples if all(passes(k, (r.get("values") or {}).get(k)) is not False for k in measure)
                                and not r.get("invalid") and not r.get("error")) / len(samples))
    out = {"nominal": {"values": base, "failing": [k for k in measure if passes(k, base.get(k)) is False]},
           "sensitivity": ranking, "monte_carlo": mc}
    if model_row is not None:
        out["model_glass_offset"] = {k: (None if _finite(model_base.get(k)) is None or _finite(base.get(k)) is None else model_base[k] - base[k])
                                     for k in measure}
    return out


def _validate_probes(plan: dict[str, Any], items: dict[str, Any], measure: list[str]) -> list[str]:
    if plan.get("target") not in items:
        raise _bad(f"probes: target must be a checked item of the demand: {sorted(items)}")
    if not str(plan.get("mechanism", "")).strip():
        raise _bad("probes: mechanism: say why the target falls short before probing")
    if plan["target"] not in measure:
        measure = [plan["target"], *measure]
    probes = plan["probes"]
    if not isinstance(probes, list) or not 1 <= len(probes) <= MAX_PROBES:
        raise _bad(f"probes: 1 to {MAX_PROBES} entries; pick the few your first-order reasoning ranks highest")
    seen = set()
    for p in probes:
        if not isinstance(p, dict) or not demand.ID_RE.match(str(p.get("id", ""))) or p["id"] in seen:
            raise _bad("each probe needs a unique id (letters, digits, _)")
        seen.add(p["id"])
        if not str(p.get("prediction", "")).strip():
            raise _bad(f"{p['id']}: write the prediction (mechanism, direction, rough size) before measuring")
        delta = p.get("delta")
        if not (isinstance(delta, (int, float)) and not isinstance(delta, bool) and math.isfinite(delta) and delta != 0):
            raise _bad(f"{p['id']}: delta must be a non-zero finite number")
        if ("script" in p) == ("key" in p):
            raise _bad(f"{p['id']}: give either key (a numeric lens parameter such as 3.radius) or script (run(w, delta))")
        if "key" in p and kit.parse_key(p["key"])[2] in ("material", "comment"):
            raise _bad(f"{p['id']}: key must be a numeric parameter")
        expect = p.get("expect") or {}
        if not isinstance(expect, dict) or any(k not in measure or v not in DIRECTIONS for k, v in expect.items()):
            raise _bad(f"{p['id']}: expect maps measured ids to one of {DIRECTIONS}")
    return measure


def _probe_items(ctx, plan: dict[str, Any]) -> list[dict[str, Any]]:
    keys = [p["key"] for p in plan["probes"] if "key" in p]
    start = kit.values(ctx, keys) if keys else {}
    missing = [k for k, v in start.items() if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)]
    if missing:
        raise _bad(f"probes: {missing} have no finite current value to perturb (a flat surface's radius: probe its curvature)")
    out = [{"i": 0, "set": {}, "probe": "base"}]
    for p in plan["probes"]:
        for sign, side in ((-1, "minus"), (1, "plus")):
            step = sign * float(p["delta"])
            item = {"i": len(out), "probe": p["id"], "side": side}
            if "key" in p:
                item["set"] = {p["key"]: float(start[p["key"]]) + step}
            else:
                item["set"], item["change"] = {}, {"script": p["script"], "delta": step}
            out.append(item)
    return out


def _finite(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


def _direction(change: float | None, scale: float) -> str | None:
    if change is None:
        return None
    if abs(change) / scale < NONE_BELOW:
        return "none"
    return "up" if change > 0 else "down"


def _probe_result(plan: dict[str, Any], scales: dict[str, float | None], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """把 2N+1 个点整理成预测与实测的对照、按对目标的效应排序，并给出该先处理的问题。
    Turn the 2N+1 points into predictions next to measurements, a ranking by effect on the target, and what to fix first."""
    target = plan["target"]
    by = {(r.get("probe"), r.get("side")): r for r in rows}
    baseline = by[("base", None)]
    base_values = baseline.get("values") or {}
    if _finite(base_values.get(target)) is None:
        # 前提不成立（例如没有实焦点、零件交叠）时，扰动出的变化没有意义；先确认可行性
        # without a valid baseline value (no real focus, overlapping parts) changes mean nothing: establish feasibility first
        return {"target": target, "baseline": baseline, "probes": [], "ranking": [],
                "review": [f"{target} has no valid value on the base lens, so no perturbation can be read; "
                           "go through the feasibility ladder first (invariants, an on-axis point under all real constraints, then the full field)"]}
    results = []
    for p in plan["probes"]:
        row: dict[str, Any] = {"id": p["id"], "control": bool(p.get("control")), "prediction": p["prediction"], "delta": p["delta"],
                               "minus": by[(p["id"], "minus")], "plus": by[(p["id"], "plus")]}
        # 斜率按 +delta 与 −delta 的差算，方向相对 +delta / the slope uses plus − minus; direction is for +delta
        observed, effect, one_sided = {}, {}, {}
        for k in plan["measure"]:
            # inf、NaN 与缺失一样算没有有效值 / inf and NaN count as no valid value, like a missing one
            lo, hi = (_finite((row[side].get("values") or {}).get(k)) for side in ("minus", "plus"))
            b = _finite(base_values.get(k))
            scale = scales.get(k) or (abs(b) if b else None) or 1.0
            if lo is not None and hi is not None:
                change = (hi - lo) / 2
            elif b is not None and (lo is None) != (hi is None):
                # 一侧没有有效值（例如越过全反射）时用另一侧对基准的差，不把整个扰动丢掉
                # one side has no valid value (e.g. past TIR): use the other side against the base instead of dropping the probe
                change = hi - b if hi is not None else b - lo
                one_sided[k] = "plus" if hi is not None else "minus"
            else:
                change = None
            observed[k] = _direction(change, scale)
            if observed[k] == "none" and k not in one_sided and b is not None and lo is not None:
                if min(lo - b, hi - b) / scale >= NONE_BELOW:
                    observed[k] = "valley"
                elif max(lo - b, hi - b) / scale <= -NONE_BELOW:
                    observed[k] = "peak"
            effect[k] = None if change is None or scales.get(k) is None else abs(change) / scales[k]
        row["observed"], row["normalised_effect"] = observed, effect
        if one_sided:
            row["one_sided"] = one_sided
        row["mismatch"] = {k: {"expected": v, "observed": observed.get(k)} for k, v in (p.get("expect") or {}).items()
                           if observed.get(k) is not None and observed[k] != v}
        results.append(row)
    movers = [r["id"] for r in results if not r["control"] and (r["normalised_effect"].get(target) or 0) >= NONE_BELOW]
    controls_moving = [r["id"] for r in results if r["control"] and (r["normalised_effect"].get(target) or 0) >= NONE_BELOW]
    mismatches = [r["id"] for r in results if r["mismatch"]]
    review = []
    extrema = [r["id"] for r in results if not r["control"] and r["observed"].get(target) in ("valley", "peak")]
    unreadable = [r["id"] for r in results if not r["control"] and r["observed"].get(target) is None]
    if not movers and unreadable:
        # 两侧都没有有效值时读不出任何效应，不能据此说结构到顶 / with no valid value on either side nothing is measured, so no structure verdict
        review.append(f"probes {unreadable} gave no valid {target} at -delta or +delta, so their effect cannot be read; "
                      "use a smaller delta or fix the compensation before calling it a structure question")
    if not movers and extrema:
        # 两侧都变差说明这些变量已在极值上，不说明结构到顶 / both sides worse means these variables are at an extremum, not that the structure is capped
        review.append(f"no probed variable moves {target} at first order, but {extrema} sit at an extremum of it (both sides move the same way): "
                      "they are already optimal here, so optimising them gains nothing; probe other variables before calling it a structure question")
    elif not movers and not unreadable:
        review.append(f"no probed variable moves {target}; if the first-order reasoning agrees, this is a structure question: ask the user")
    if not any(r["control"] for r in results):
        # 阴性对照：原理上不该动目标的变量；没有它，排名里的小效应无法和测量噪声区分
        # a negative control: a variable that in principle should not move the target; without one, small effects cannot be told from noise
        review.append(f"no control probe: small effects on {target} in the ranking have no null to compare with; add a control when the ranking decides what to optimise")
    if controls_moving:
        review.append(f"control probes {controls_moving} move {target}; recheck the model or the measurement before trusting the ranking")
    if mismatches:
        review.append(f"predictions failed for {mismatches}; revise your understanding of the system before optimising")
    if plan.get("compensate") == "quick_focus":
        comps = [r.get("compensation") or {} for r in rows]
        if all(c.get("thickness_after") == c.get("thickness_before") for c in comps):
            # 实测：fiber 起点镜头上 Quick Focus 从任何厚度出发都不动，Cooke 样例上正常；不能默认补偿生效
            # measured: on the fiber start lens Quick Focus never moved, on the Cooke sample it worked; never assume the compensation acted
            review.append(f"quick_focus never moved surface {comps[0].get('surface')} in any point, so nothing was compensated; "
                          "write a compensate script for this system (for example re-solve the working distance) and probe again")
    ranking = sorted((r for r in results if not r["control"]), key=lambda r: -(r["normalised_effect"].get(target) or 0))
    return {"target": target, "baseline": baseline, "probes": results,
            "ranking": [{"id": r["id"], "effect": r["normalised_effect"].get(target)} for r in ranking], "review": review}


# ---------- worker 侧 / worker side ----------

def _script_run(w, rel: str):
    def load():
        path = w.path(rel)
        namespace: dict[str, Any] = {"__name__": f"saos_scan_{path.stem}", "__file__": str(path)}
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)
        return namespace["run"]

    return load()


def _prepare(w, plan: dict[str, Any], values: dict[str, Any], change: dict[str, Any] | None = None) -> dict[str, Any]:
    """写入 fixed 和这个点的参数（扰动脚本在这之后运行），再求解、补偿。返回求解与补偿的记录；求解失败时带 invalid。
    Write fixed and the point's parameters (then a probe's change script), solve and compensate. Returns their records,
    with invalid when the solve fails."""
    out: dict[str, Any] = {}
    w.set(plan.get("fixed") or {})
    w.set(values)
    if change is not None:
        _script_run(w, change["script"])(w, change["delta"])
    solve = plan.get("solve")
    if solve is not None:
        target = solve["target"]

        def f(x: float):
            w.set({solve["var"]: x})
            w.cache.clear()
            if "item" in target:
                return w.measure([target["item"]])["values"][target["item"]]
            name, *rest = target["operand"]
            return w.op(name, *rest)

        res = search.root(f, float(solve["lo"]), float(solve["hi"]), target=float(solve.get("value", 0.0)), scan=int(solve.get("scan", 9)),
                          xtol=solve.get("xtol"), should_stop=w.cancel_requested)
        out["solve"] = {k: res[k] for k in ("x", "f", "ok", "evals", "reason")}
        if not res["ok"]:
            out["invalid"] = f"solve: {res['reason']}"
            return out
        w.set({solve["var"]: res["x"]})
    comp = plan.get("compensate")
    if comp == "quick_focus":
        out["compensation"] = w.quick_focus()
    elif isinstance(comp, dict):
        out["compensation"] = _script_run(w, comp["script"])(w)
    w.cache.clear()
    return out


def _measure(w, plan: dict[str, Any]) -> dict[str, Any]:
    m = w.measure(plan["measure"])
    row = {k: m[k] for k in ("values", "shortfall", "hard_failing", "invalid", "errors")}
    if plan.get("operands"):
        row["operands"] = {}
        for name, spec in plan["operands"].items():
            try:
                row["operands"][name] = w.op(spec[0], *spec[1:])
            except Exception as exc:  # noqa: BLE001 - 一个附加操作数出错不让整个点作废 / one extra operand failing does not void the point
                row["operands"][name] = f"error: {exc}"
    return row


def _save(w, item: dict[str, Any], row: dict[str, Any]) -> None:
    if w.args.get("lens_dir") and isinstance(row.get("shortfall"), float) and math.isfinite(row["shortfall"]):
        rel = f"{w.args['lens_dir']}/p{item['i']:03d}.zmx"
        target = w.path(f"lenses/{rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        w.backend.save_as(target)
        row["lens"] = rel


def point(w, item: dict[str, Any]) -> dict[str, Any]:
    """一个网格点或列表点。/ One grid or list point."""
    plan = w.args["plan"]
    row: dict[str, Any] = {k: item[k] for k in ("i", "set", "probe", "side", "deviation", "sample", "deviations") if k in item}
    row.update(_prepare(w, plan, item["set"], item.get("change")))
    if "invalid" in row:
        row["shortfall"] = math.inf
        return row
    row.update(_measure(w, plan))
    _save(w, item, row)
    return row


def descend(w, item: dict[str, Any]) -> dict[str, Any]:
    """从一个起点做无导数寻优，目标是所测各项的目标差距（需求单的定义）。/ Minimise the demand's shortfall from one start."""
    plan = w.args["plan"]
    m = plan["minimize"]
    specs = m["vars"]

    def f(p: dict[str, float]):
        w.reload()
        extra = _prepare(w, plan, p)
        if "invalid" in extra:
            return None
        g = w.measure(plan["measure"])["shortfall"]
        return g if math.isfinite(g) else None

    res = search.minimize(f, item["start"], {k: s["step"] for k, s in specs.items()},
                          bounds={k: (s.get("min"), s.get("max")) for k, s in specs.items()}, method=m.get("method", "nelder-mead"),
                          max_evals=int(m.get("max_evals", 200)), max_seconds=float(m.get("max_seconds", 900)),
                          xtol=float(m.get("xtol", 1e-3)), should_stop=w.cancel_requested)
    row: dict[str, Any] = {"i": item["i"], "start": item["start"], "set": res["best"], "start_f": res["start_f"], "best_f": res["best_f"],
                           "evals": res["evals"], "stopped": res["stopped"], "seconds": res["seconds"],
                           "history": [[h["f"], h["x"]] for h in res["history"]]}
    if res["best"] is None:
        row["shortfall"] = math.inf
        row["invalid"] = "no valid point reached"
        return row
    w.reload()
    row.update(_prepare(w, plan, res["best"]))
    row.update(_measure(w, plan))
    _save(w, item, row)
    return row


# ---------- 后台进程侧 / daemon side ----------

def run(ctx, plan: dict[str, Any], workers: int | None = None, detach: bool = False) -> dict[str, Any]:
    """准备（校验、目录、镜头快照）在本请求里完成；detach 时其余放进后台线程，立即返回，后台进程继续处理其他命令。
    Preparation (validation, folder, lens snapshot) happens in this request; with detach the rest runs in a background
    thread and the call returns at once, so the daemon keeps serving other commands."""
    from saos import config, parallel, workflow

    d = demand.load_job(ctx.job.root)
    plan = validate(plan, d, ctx.job.root)
    exploring = plan["mode"] == "candidates"
    # 结构探索的候选是各自的文件，不用当前镜头 / exploration candidates are files of their own; the current lens is not used
    if not exploring and ctx.lens is None:
        raise SaosError(ErrorCode.NO_LENS_LOADED, f"job {ctx.job.id} has no lens loaded")
    if not exploring and ctx.dirty:
        raise SaosError(ErrorCode.UNSAVED_CHANGES, "scan workers load the saved lens file; save your changes first (lens save)")
    post = None
    if plan["mode"] == "probes":
        checked = {i["id"]: i for i in demand.checked(d)}
        scales = {k: None if checked[k]["goal"] is None else float(checked[k].get("scale", demand._default_scale(checked[k]["goal"])))
                  for k in plan["measure"]}
        post = lambda rows: _probe_result(plan, scales, rows)  # noqa: E731
    if plan["mode"] == "tolerance":
        checked = {i["id"]: i for i in demand.checked(d)}
        post = lambda rows: _tolerance_result(plan, checked, rows)  # noqa: E731
    if plan["mode"] in ("minimize", "candidates", "tolerance"):
        # 寻优、结构探索和公差可能跑很久，公差还要按确认过的判定算：与 opt local 同样先确认需求
        # minimisation, exploration and tolerancing may run long, and tolerancing judges by the confirmed checks: same gate as opt local
        workflow.preflight(ctx.job.root)
    if plan["mode"] == "vars":
        items = [{"i": k, "set": p} for k, p in enumerate(search.grid(plan["vars"]))]
    elif plan["mode"] == "points":
        items = [{"i": k, "set": p} for k, p in enumerate(plan["points"])]
    elif plan["mode"] == "probes":
        items = _probe_items(ctx, plan)
    elif plan["mode"] == "tolerance":
        items = _tolerance_items(ctx, plan)
    elif exploring:
        items = [{"i": k} for k in range(len(plan["candidates"]))]
    else:
        starts = plan["minimize"].get("starts", "current")
        if starts == "current":
            starts = [kit.values(ctx, list(plan["minimize"]["vars"]))]
        items = [{"i": k, "start": s} for k, s in enumerate(starts)]
    count = min(len(items), int(workers or plan.get("workers") or parallel.DEFAULT_WORKERS), config.compute_cores())
    pool = ctx._daemon.pool
    if not pool.lock.acquire(timeout=2):
        raise SaosError(ErrorCode.POOL_BUSY, "the worker pool is busy with a detached scan; wait for it (saos scan show) or cancel it (saos job cancel)",
                        {"pool": pool.status()})
    try:
        root = ctx.path(SCANS_DIR)
        root.mkdir(exist_ok=True)
        folder = root / f"{jobs.next_archive_number(root):03d}"
        folder.mkdir()
        args = {"plan": plan, "lens_dir": f"scans/{folder.name}", "cancel_file": str(folder / jobs.CANCEL_FILE)}
        if exploring:
            items = candidates.snapshot(ctx.job.root, plan, folder)
            # 每个候选自己加载或搭建，worker 不先加载公共镜头 / each candidate loads or builds its own; no shared lens first
            base = None
            # 几个优化器同时跑时分掉可用核，不互相抢 / concurrent optimisers share the usable cores instead of fighting over them
            args["cores"] = max(1, config.compute_cores() // count)
            write_json(folder / "plan.json", {**plan, "workers": count, "cores_per_worker": args["cores"],
                                              "budget_s": candidates.budget_s(plan, count)})
        else:
            write_json(folder / "plan.json", {**plan, "lens": ctx.job.relative_lens(ctx.lens), "workers": count})
            # 镜头快照：分离运行期间改动或保存工作镜头不影响扫描 / a snapshot, so later edits of the working lens do not reach the scan
            base = folder / f"base{ctx.lens.suffix}"
            shutil.copy2(ctx.lens, base)
    except BaseException:
        pool.lock.release()
        raise
    job = ctx.job
    if not detach:
        try:
            return _execute(job, folder, plan, items, pool, base, count, args,
                            progress=lambda stage: job.write_progress(stage=stage), cancelled=job.cancel_requested, post=post)
        finally:
            pool.lock.release()
    progress_file = folder / jobs.PROGRESS_FILE
    write_json(progress_file, {"op": "scan.run", "status": "running", "detached": True, "points": len(items), "workers": count,
                               "started": jobs.now_iso(), "daemon_pid": os.getpid()})

    def report(**fields: Any) -> None:
        try:
            current = json.loads(progress_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = {}
        write_json(progress_file, {**current, **fields, "updated": jobs.now_iso()})

    def body() -> None:
        try:
            summary = _execute(job, folder, plan, items, pool, base, count, args, progress=lambda stage: report(stage=stage),
                               cancelled=lambda: job.cancel_requested() or (folder / jobs.CANCEL_FILE).is_file(), on_done=report, post=post)
            log.info("detached scan %s finished: %s valid of %s", folder.name, summary["valid"], summary["points"])
        except Exception as exc:  # noqa: BLE001 - 后台线程不能让后台进程退出 / a background thread must not take the daemon down
            log.exception("detached scan %s failed", folder.name)
            report(status="failed", error=f"{type(exc).__name__}: {exc}"[:1000])
        finally:
            pool.lock.release()

    thread = threading.Thread(target=body, name=f"scan-{folder.name}", daemon=True)
    ctx._daemon.background[folder.relative_to(job.root).as_posix()] = thread
    thread.start()
    out = {"folder": folder.relative_to(job.root).as_posix(), "detached": True, "points": len(items), "workers": count,
           "follow": f"saos scan --job {job.id} --show {folder.name}", "cancel": f"saos job cancel {job.id}"}
    if exploring:
        out["budget_s"] = candidates.budget_s(plan, count)
    return out


def _execute(job: jobs.Job, folder, plan: dict[str, Any], items: list[dict[str, Any]], pool, base, count: int, args: dict[str, Any],
             progress, cancelled, on_done=None, post=None) -> dict[str, Any]:
    from saos import parallel

    fn = {"minimize": "saos.scan:descend", "candidates": "saos.candidates:candidate"}.get(plan["mode"], "saos.scan:point")
    out = parallel.dispatch(pool, job, base, fn, None, items, count, True, args, progress, cancelled)
    if not out["workers_available"]:
        raise SaosError(ErrorCode.ZOS_ERROR, f"no parallel worker could start: {out['startup']['unavailable']}", {"pool": pool.status()})
    rows = []
    for item, res in zip(items, out["results"]):
        if res is None:
            res = {**item, "shortfall": math.inf, "error": out["errors"].get(str(item["i"]), "not run")}
        rows.append(res)
    if plan["mode"] == "candidates":
        ranked, kept_ids = candidates.rank(rows, plan["keep"])
        keep = {r.get("lens") for r in ranked if r["id"] in kept_ids and r.get("lens")}
    else:
        ranked = search.ranked(rows)
        keep = {r.get("lens") for r in ranked[:max(0, plan["keep"])] if r.get("lens")}
    lens_root = job.lenses_dir / args["lens_dir"]
    for r in ranked:
        if r.get("lens") and r["lens"] not in keep:
            (job.lenses_dir / r["lens"]).unlink(missing_ok=True)
            r["lens"] = None
    if lens_root.is_dir() and not any(lens_root.iterdir()):
        shutil.rmtree(lens_root, ignore_errors=True)
    valid = [r for r in ranked if isinstance(r.get("shortfall"), (int, float)) and math.isfinite(r["shortfall"])]
    result = {"folder": folder.relative_to(job.root).as_posix(), "mode": plan["mode"], "points": len(items), "valid": len(valid),
              "errors": out["errors"], "not_run": out["not_run"], "cancelled": out["cancelled"],
              "workers": out["workers"], "startup": out["startup"], "elapsed_s": out["elapsed_s"],
              "best": ranked[0] if valid else None, "kept_lenses": sorted(k for k in keep if k), "rows": ranked}
    if post is not None and not out["cancelled"]:
        result.update(post(rows))
    if plan["mode"] == "candidates":
        result["best"] = next((r for r in ranked if r.get("valid")), None)
        result["kept"] = [r["id"] for r in ranked if r.get("kept")]
        result["budget_limited"] = [r["id"] for r in ranked if r.get("still_improving")]
        result["page"] = candidates.page(job, folder, plan, ranked, plan.get("lang")).relative_to(job.root).as_posix()
    write_json(folder / "result.json", result)
    (folder / jobs.CANCEL_FILE).unlink(missing_ok=True)
    if on_done is not None:
        on_done(status="cancelled" if out["cancelled"] else "done", valid=len(valid), elapsed_s=out["elapsed_s"])
    job.seal(folder)
    return summarize(result)


def summarize(result: dict[str, Any]) -> dict[str, Any]:
    """返回给 agent 的摘要：前 10 行，不含寻优历史。/ The summary for the agent: the top 10 rows, no search history."""
    summary = {k: v for k, v in result.items() if k != "rows"}
    if result.get("mode") == "candidates":
        keys = ("id", "valid", "kept", "still_improving", "start_shortfall", "shortfall", "problems", "hard_failing", "values", "lens", "error")
    else:
        keys = ("i", "set", "shortfall", "hard_failing", "invalid", "values", "solve", "lens", "operands", "error")
    summary["top"] = [{k: r.get(k) for k in keys} for r in result["rows"][:10]]
    if summary["best"] is not None:
        # 寻优历史和逐段记录只在 result.json 里 / search history and per-stage records stay in result.json
        summary["best"] = {k: v for k, v in summary["best"].items() if k not in ("history", "stages", "start")}
    summary["rows_in"] = f"{result['folder']}/result.json"
    return summary


def show(job: jobs.Job, scan: str | None = None) -> dict[str, Any]:
    """一次扫描的状态或结果，只读文件，后台进程忙时也能用。/ The state or result of a scan from its files; works while the daemon is busy."""
    root = job.root / SCANS_DIR
    names = sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    if not names:
        raise SaosError(ErrorCode.FILE_NOT_FOUND, f"job {job.id} has no scans")
    name = f"{int(scan):03d}" if scan is not None and str(scan).isdigit() else (scan or names[-1])
    folder = root / str(name)
    if not folder.is_dir():
        raise SaosError(ErrorCode.FILE_NOT_FOUND, f"no scan {name}", {"scans": names})
    if (folder / "result.json").is_file():
        return {"finished": True, **summarize(json.loads((folder / "result.json").read_text(encoding="utf-8")))}
    progress = json.loads((folder / jobs.PROGRESS_FILE).read_text(encoding="utf-8")) if (folder / jobs.PROGRESS_FILE).is_file() else None
    running = any(r["folder"] == f"{SCANS_DIR}/{name}" for r in jobs.detached_runs(job.root))
    out = {"folder": f"{SCANS_DIR}/{name}", "finished": False, "running": running, "progress": progress}
    if not running and not (progress or {}).get("error"):
        out["note"] = "no result and no running daemon owns this scan; it was interrupted"
    return out
