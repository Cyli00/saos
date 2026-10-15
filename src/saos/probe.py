"""扰动诊断（probe）：按 agent 事先写下的预测，逐个扰动少数变量，带补偿后实测，与预测对照。
Perturbation probe: perturb a few variables one at a time, compensate, measure, and compare with the predictions
the agent wrote down beforehand.

    saos probe --job J --plan plan.json

plan.json：
    {"target": "spot",                       # 当前最大差距的那一项（需求单中的 id）/ the item with the largest gap
     "mechanism": "Petzval 场曲 …",           # 它为什么差：机理 / why it falls short
     "compensate": "quick_focus",            # null | "quick_focus" | {"script": "probes/refocus.py"}
     "measure": ["spot", "working_distance"],# 可省略，默认测需求单里所有带界限的项 / default: every bounded item
     "probes": [{"id": "l1_rear_r", "surface": 3, "param": "radius", "delta": 0.02,
                 "prediction": "…", "expect": {"spot": "down"}},
                {"id": "l2_asph", "script": "probes/l2_a4.py", "delta": 1e-3, "control": true,
                 "prediction": "只改像散分配，不改 Petzval 和", "expect": {"spot": "none"}}]}

每个扰动在基准镜头上取 −delta 和 +delta 各测一次；改动方式是内置参数（radius、thickness、conic），
或一个脚本 run(ctx, delta)。每次都从同一份基准副本重新加载，结束后恢复，镜头内容不变。
Each probe is measured at −delta and +delta from the same base copy; the change is a built-in parameter or a script
run(ctx, delta). The base copy is reloaded for every trial and restored at the end, so the lens is left unchanged.
"""

from __future__ import annotations

import math
from typing import Any

from saos import demand, jobs
from saos.backend.base import is_par
from saos.protocol import ErrorCode, SaosError, write_json

PROBES_DIR = "probes"
PARAMS = ("radius", "thickness", "conic", "par<N>")
DIRECTIONS = ("up", "down", "none")
MAX_PROBES = 6
# 归一化变化（Δ/scale）小于这个值算"没有变化" / a normalised change below this counts as "none"
NONE_BELOW = 0.05


def _bad(message: str) -> SaosError:
    return SaosError(ErrorCode.BAD_ARGS, f"probe plan: {message}")


def validate(plan: Any, d: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise _bad("must be a JSON object")
    items = {i["id"]: i for i in demand.checked(d)}
    if plan.get("target") not in items:
        raise _bad(f"target must be a checked item of the demand: {sorted(items)}")
    if not str(plan.get("mechanism", "")).strip():
        raise _bad("mechanism: say why the target falls short before probing")
    measure = plan.get("measure") or [i for i, item in items.items() if item["goal"] is not None or i == plan["target"]]
    if plan["target"] not in measure:
        measure = [plan["target"], *measure]
    unknown = [m for m in measure if m not in items]
    if unknown:
        raise _bad(f"measure has ids that are not checks of the demand: {unknown}")
    comp = plan.get("compensate")
    if not (comp is None or comp == "quick_focus" or (isinstance(comp, dict) and set(comp) == {"script"})):
        raise _bad('compensate must be null, "quick_focus" or {"script": "<path in the job>"}')
    probes = plan.get("probes")
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
        if "script" in p:
            if "surface" in p or "param" in p:
                raise _bad(f"{p['id']}: give either script or surface + param")
        elif not (isinstance(p.get("surface"), int) and (p.get("param") in PARAMS[:3] or is_par(str(p.get("param"))))):
            raise _bad(f"{p['id']}: give surface (int) and param ({', '.join(PARAMS)}), or a script")
        expect = p.get("expect") or {}
        if not isinstance(expect, dict) or any(k not in measure or v not in DIRECTIONS for k, v in expect.items()):
            raise _bad(f"{p['id']}: expect maps measured ids to one of {DIRECTIONS}")
    return {**plan, "measure": measure, "compensate": comp}


def _load_script(ctx, rel: str, folder, name: str):
    path = ctx.path(rel)
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SaosError(ErrorCode.FILE_NOT_FOUND, f"cannot read {rel}: {exc}") from exc
    copy = folder / f"{name}.py"
    copy.write_text(source, encoding="utf-8")
    namespace: dict[str, Any] = {"__name__": f"saos_probe_{name}", "__file__": str(path)}
    exec(compile(source, str(copy), "exec"), namespace)
    if not callable(namespace.get("run")):
        raise SaosError(ErrorCode.BAD_ARGS, f"{rel} must define run(ctx, ...)")
    return namespace["run"]


def _scale(item: dict[str, Any]) -> float | None:
    if item["goal"] is None:
        return None
    return float(item.get("scale", demand._default_scale(item["goal"])))


def _direction(change: float | None, item: dict[str, Any], base: float | None) -> str | None:
    if change is None:
        return None
    scale = _scale(item) or (abs(base) if base else None) or 1.0
    if abs(change) / scale < NONE_BELOW:
        return "none"
    return "up" if change > 0 else "down"


def run(ctx, plan: dict[str, Any]) -> dict[str, Any]:
    """按计划逐个扰动、补偿、实测；返回预测与实测的对照。不改动当前镜头。
    Perturb, compensate and measure probe by probe; return predictions next to measurements. The lens is left unchanged."""
    from saos import workflow

    if ctx.lens is None:
        raise SaosError(ErrorCode.NO_LENS_LOADED, f"job {ctx.job.id} has no lens loaded")
    d = demand.load_job(ctx.job.root)
    plan = validate(plan, d)
    items = {i["id"]: i for i in demand.checked(d)}
    measured_items = [items[m] for m in plan["measure"]]
    root = ctx.path(PROBES_DIR)
    root.mkdir(exist_ok=True)
    folder = root / f"{jobs.next_archive_number(root):03d}"
    folder.mkdir()
    write_json(folder / "plan.json", plan)
    _, before_sha = workflow.lens_state(ctx)
    base = folder / "base.zmx"
    ctx.backend.save_as(base)
    scripts = demand.prepare_scripts(ctx.job.root, d)
    compensate = plan["compensate"]
    comp_run = _load_script(ctx, compensate["script"], folder, "compensate") if isinstance(compensate, dict) else None

    def trial(name: str, apply=None) -> dict[str, Any]:
        if ctx.job.cancel_requested():
            raise SaosError(ErrorCode.CANCELLED, f"probe cancelled before {name}; finished trials are in {folder.relative_to(ctx.job.root).as_posix()}")
        ctx.job.write_progress(stage=f"probe {name}")
        ctx.backend.load(base)
        if apply is not None:
            apply()
        comp = None
        if compensate == "quick_focus":
            comp = ctx.backend.quick_focus()
        elif comp_run is not None:
            comp = comp_run(ctx)
        sub = folder / name
        sub.mkdir()
        values = {}
        for item in measured_items:
            item_dir = sub / item["id"]
            item_dir.mkdir()
            values[item["id"]] = demand.measure(ctx, item, item_dir, scripts)["value"]
        gaps = {i["id"]: demand.shortfall(i, values[i["id"]]) for i in measured_items if i["goal"] is not None}
        g = sum(items[k]["weight"] * v for k, v in gaps.items())
        hard = [k for k, v in gaps.items() if items[k]["kind"] in demand.HARD_KINDS and v > 0]
        return {"values": values, "shortfall": g, "hard_failing": hard, "compensation": comp}

    try:
        baseline = trial("base")
        results = []
        if baseline["values"][plan["target"]] is None:
            # 前提不成立（例如没有实焦点、零件交叠）时，扰动出的变化没有意义；先确认可行性
            # without a valid baseline value (no real focus, overlapping parts) changes mean nothing: establish feasibility first
            result = {"folder": folder.relative_to(ctx.job.root).as_posix(), "target": plan["target"], "baseline": baseline, "probes": [],
                      "ranking": [], "lens_unchanged": True,  # finally 里重新加载基准副本 / the base copy is reloaded in finally
                      "review": [f"{plan['target']} has no valid value on the base lens, so no perturbation can be read; "
                                 "go through the feasibility ladder first (invariants, an on-axis point under all real constraints, then the full field)"]}
            write_json(folder / "result.json", result)
            ctx.job.seal(folder)
            return result
        for p in plan["probes"]:
            row: dict[str, Any] = {"id": p["id"], "control": bool(p.get("control")), "prediction": p["prediction"], "delta": p["delta"]}
            change_run = _load_script(ctx, p["script"], folder, f"change-{p['id']}") if "script" in p else None
            start = None if change_run else ctx.backend.surface_value(p["surface"], p["param"])
            for sign, label in ((-1, "minus"), (1, "plus")):
                step = sign * float(p["delta"])
                if change_run:
                    apply = lambda step=step: change_run(ctx, step)  # noqa: E731
                else:
                    apply = lambda step=step: ctx.backend.set_surface(p["surface"], p["param"], float(start) + step)  # noqa: E731
                row[label] = trial(f"{p['id']}-{label}", apply)
            # 斜率按 +delta 与 −delta 的差算，方向相对 +delta / the slope uses plus − minus; direction is for +delta
            observed, effect = {}, {}
            for item in measured_items:
                lo, hi = row["minus"]["values"][item["id"]], row["plus"]["values"][item["id"]]
                change = None if lo is None or hi is None else (hi - lo) / 2
                observed[item["id"]] = _direction(change, item, baseline["values"][item["id"]])
                scale = _scale(item)
                effect[item["id"]] = None if change is None or scale is None else abs(change) / scale
            row["observed"] = observed
            row["normalised_effect"] = effect
            row["mismatch"] = {k: {"expected": v, "observed": observed.get(k)} for k, v in (p.get("expect") or {}).items()
                               if observed.get(k) is not None and observed[k] != v}
            results.append(row)
    finally:
        # 不论成败都回到基准内容；路径和未保存状态仍由后台进程记着
        # back to the base content whatever happened; the daemon still tracks the path and the unsaved state
        ctx.backend.load(base)
    _, after_sha = workflow.lens_state(ctx)
    target = plan["target"]
    movers = [r["id"] for r in results if not r["control"] and (r["normalised_effect"].get(target) or 0) >= NONE_BELOW]
    controls_moving = [r["id"] for r in results if r["control"] and (r["normalised_effect"].get(target) or 0) >= NONE_BELOW]
    mismatches = [r["id"] for r in results if r["mismatch"]]
    review = []
    if not movers:
        review.append(f"no probed variable moves {target}; if the first-order reasoning agrees, this is a structure question: ask the user")
    if not any(r["control"] for r in results):
        # 阴性对照：原理上不该动目标的变量；没有它，排名里的小效应无法和测量噪声区分
        # a negative control: a variable that in principle should not move the target; without one, small effects cannot be told from noise
        review.append(f"no control probe: small effects on {target} in the ranking have no null to compare with; add a control when the ranking decides what to optimise")
    if controls_moving:
        review.append(f"control probes {controls_moving} move {target}; recheck the model or the measurement before trusting the ranking")
    if mismatches:
        review.append(f"predictions failed for {mismatches}; revise your understanding of the system before optimising")
    if compensate == "quick_focus":
        trials = [baseline] + [r[side] for r in results for side in ("minus", "plus")]
        if all(t["compensation"]["thickness_after"] == t["compensation"]["thickness_before"] for t in trials):
            # 实测：fiber 起点镜头上 Quick Focus 从任何厚度出发都不动，Cooke 样例上正常；不能默认补偿生效
            # measured: on the fiber start lens Quick Focus never moved, on the Cooke sample it worked; never assume the compensation acted
            surface = baseline["compensation"]["surface"]
            review.append(f"quick_focus never moved surface {surface} in any trial, so nothing was compensated; "
                          "write a compensate script for this system (for example re-solve the working distance) and probe again")
    ranking = sorted((r for r in results if not r["control"]), key=lambda r: -(r["normalised_effect"].get(target) or 0))
    result = {"folder": folder.relative_to(ctx.job.root).as_posix(), "target": target, "baseline": baseline, "probes": results,
              "ranking": [{"id": r["id"], "effect": r["normalised_effect"].get(target)} for r in ranking],
              "review": review, "lens_unchanged": before_sha == after_sha}
    write_json(folder / "result.json", result)
    ctx.job.seal(folder)
    return result
