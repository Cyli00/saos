"""停机参数校准的正式实验：实验清单、样本准入、参数选择与验证（DESIGN.md 实现顺序第 5 步）。

与 bench.py 分开：bench.py 负责按参考策略跑出完整轨迹；本模块决定哪些轨迹算正式样本，
以及按事先冻结的规则从候选参数中选择、在独立验证组上检验。规则见 docs/step5-calibration.md 第 2 节。
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from zea import config, library, rounds
from zea.protocol import ErrorCode, ZeaError

OBSERVE_ROUNDS = 20  # 正式样本必须完整观察的轮数（或在此之前通过）
GRID_N = (2, 3, 4, 5, 6)
GRID_REL = (0.005, 0.01, 0.02, 0.05)
GRID_CAP = (10, 15, 20)
INITIAL = (3, 0.01, 20)
REPORT_REL = 0.01  # 跨候选统一的"错过的改善"门槛
QUICK_ROUNDS = 2

MAIN_DEFAULT_SEEDS = (1, 2, 3)
MAIN_HARD_SEEDS = (1, 2)
HARD = {"radius_rel": 0.08, "thickness_rel": 0.24}
STRESS_STRETCHES = ({"wd": 1.15, "length": 0.9}, {"wd": 1.25}, {"length": 0.85})


def calibration_dir() -> Path:
    return library.library_dir() / "calibration"


def manifest_path(batch: str) -> Path:
    if not batch or any(c not in library.ENTRY_ID_CHARS for c in batch):
        raise ZeaError(ErrorCode.BAD_ARGS, f"批次编号只能含字母、数字和 -_.：{batch!r}")
    return calibration_dir() / f"{batch}.json"


# ---- 实验清单 ----


def plan(batch: str, policy: str) -> dict[str, Any]:
    """生成正式批次的实验清单。任务在运行前全部列出，事后不按成败增删。"""
    split = json.loads((library.library_dir() / "benchmarks.json").read_text(encoding="utf-8"))
    base = split["perturbation"]
    tasks = []
    for group in ("tuning", "validation"):
        for entry in split["groups"][group]:
            patent = (library.load_entry(entry).get("source") or {}).get("patent", entry)
            common = {"group": group, "entry": entry, "patent": patent}
            for seed in MAIN_DEFAULT_SEEDS:
                tasks.append({**common, "class": "main", "tier": "default", "seed": seed, "radius_rel": base["radius_rel"], "thickness_rel": base["thickness_rel"], "amplify": True, "stretch": {}})
            for seed in MAIN_HARD_SEEDS:
                tasks.append({**common, "class": "main", "tier": "hard", "seed": seed, **HARD, "amplify": False, "stretch": {}})
            for st in STRESS_STRETCHES:
                tasks.append({**common, "class": "stress", "tier": "stretch", "seed": 1, "radius_rel": 0.0, "thickness_rel": 0.0, "amplify": False, "stretch": st})
    for k, t in enumerate(tasks):
        tag = t["tier"] if not t["stretch"] else "stretch-" + "-".join(f"{a}{round(b * 100)}" for a, b in sorted(t["stretch"].items()))
        t["task"] = f"{k:03d}-{t['entry']}-{tag}-s{t['seed']}"
        t["inputs"] = task_inputs(t["entry"], t["stretch"])
    return {
        "batch": batch,
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "policy": policy,
        "code_sha256": config.code_sha256(),
        "observe_rounds": OBSERVE_ROUNDS,
        "mode": "standalone",
        "tasks": tasks,
    }


def effective_spec(entry: str, stretch: dict[str, float]) -> dict[str, Any]:
    """任务实际使用的光学规格：库中基准规格，拉伸规格时按原结构实测值改界限。"""
    import yaml

    from zea import bench

    spec = yaml.safe_load((library.entry_dir(entry) / "benchmark.yaml").read_text(encoding="utf-8"))
    return bench.stretched_spec(spec, library.load_entry(entry)["metrics"], stretch) if stretch else spec


def task_inputs(entry: str, stretch: dict[str, float]) -> dict[str, str]:
    """实验输入的指纹：处方、库中基准规格文件，以及任务实际规格推出的完整验收配置。

    代码固定不等于输入固定：运行和准入时都按这些哈希核对，库文件被改过就拒绝。"""
    from zea import acceptance
    from zea.spec import parse_spec

    d = library.entry_dir(entry)
    config_ = acceptance.acceptance_config(parse_spec(effective_spec(entry, stretch)))
    return {
        "prescription_sha256": sha_file(d / "prescription.json"),
        "benchmark_sha256": sha_file(d / "benchmark.yaml"),
        "acceptance_config_sha256": acceptance.config_hash(config_),
    }


def write_plan(batch: str, policy: str) -> dict[str, Any]:
    path = manifest_path(batch)
    if path.exists():
        raise ZeaError(ErrorCode.FILE_EXISTS, f"实验清单已存在，不能覆盖：{path}")
    data = plan(batch, policy)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def load_plan(batch: str) -> dict[str, Any]:
    path = manifest_path(batch)
    if not path.is_file():
        raise ZeaError(ErrorCode.FILE_NOT_FOUND, f"没有实验清单：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def task_of(manifest: dict[str, Any], task_id: str) -> dict[str, Any]:
    task = next((t for t in manifest["tasks"] if t["task"] == task_id), None)
    if task is None:
        raise ZeaError(ErrorCode.BAD_ARGS, f"清单 {manifest['batch']} 中没有任务 {task_id}")
    return task


# ---- 样本准入 ----


def sha_file(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def find_jobs(homes: list[Path], batch: str) -> dict[str, list[Path]]:
    """各 ZEA_HOME 下属于该批次的任务目录：清单任务编号 → 按创建时间排序的任务目录。"""
    out: dict[str, list[Path]] = {}
    for home in homes:
        for state_file in (Path(home) / "jobs").glob("*/job.json"):
            state = json.loads(state_file.read_text(encoding="utf-8"))
            b = state.get("bench") or {}
            if b.get("batch") == batch:
                out.setdefault(b["task"], []).append(state_file.parent)
    for k in out:
        out[k].sort(key=lambda d: d.name)
    return out


def audit(job_dir: Path, manifest: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    """一个正式任务的轨迹是否合格；不合格时给出全部原因。"""
    from zea import acceptance

    reasons: list[str] = []
    state = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    b = state.get("bench") or {}
    entries = rounds.load(job_dir / "bench.jsonl")
    hist = [h for h in entries if "event" not in h]
    recorded = rounds.load(job_dir / "rounds.jsonl")
    frozen = state.get("frozen") or {}
    inputs = task.get("inputs") or {}

    if b.get("policy") != manifest["policy"] or any(h.get("policy") != manifest["policy"] for h in hist):
        reasons.append("策略版本与清单不一致")
    for key in ("entry", "seed", "radius_rel", "thickness_rel", "stretch"):
        if b.get(key) != task.get(key) and not (key == "stretch" and not b.get(key) and not task.get(key)):
            reasons.append(f"任务参数 {key} 与清单不一致")
    if any(h.get("daemon_code") != manifest["code_sha256"] for h in hist):
        reasons.append("后台进程代码版本与清单不一致或未记录")
    if b.get("client_code") != manifest["code_sha256"]:
        reasons.append("客户端代码版本与清单不一致或未记录")
    if any(h.get("mode") != manifest["mode"] for h in hist):
        reasons.append(f"运行模式不是 {manifest['mode']} 或未记录")

    # 输入：任务里保存的处方副本与清单一致；冻结的验收配置文件实际内容的哈希等于冻结值，也等于清单
    if not inputs or sha_file(job_dir / "sources" / "prescription.json") != inputs.get("prescription_sha256"):
        reasons.append("处方与清单记录的输入不一致或缺失")
    frozen_file = job_dir / (frozen.get("config") or "spec/frozen_config.json")
    if not frozen.get("sha256"):
        reasons.append("验收口径没有冻结")
    elif not frozen_file.is_file():
        reasons.append("冻结的验收配置文件缺失")
    else:
        actual = acceptance.config_hash(json.loads(frozen_file.read_text(encoding="utf-8")))
        if actual != frozen["sha256"]:
            reasons.append("冻结配置文件的实际哈希与冻结值不同")
        if frozen["sha256"] != inputs.get("acceptance_config_sha256"):
            reasons.append("冻结的验收配置与清单记录的不一致")
    if frozen.get("sha256") and any(r.get("config_sha256") != frozen["sha256"] for r in recorded):
        reasons.append("有轮次的验收配置哈希与冻结值不同")
    spec_file = job_dir / "spec" / "benchmark.yaml"
    if not spec_file.is_file():
        reasons.append("规格文件缺失")
    else:
        # 归档规格与计算记录必须一致：从任务实际规格重新生成验收配置，与冻结值和清单比较
        from zea.spec import load_spec

        try:
            spec_hash = acceptance.config_hash(acceptance.acceptance_config(load_spec(spec_file)))
        except Exception as exc:  # noqa: BLE001 - 任何解析失败都拒绝准入
            reasons.append(f"规格文件无法解析：{exc}")
        else:
            if spec_hash != frozen.get("sha256"):
                reasons.append("任务规格生成的验收配置与冻结值不同")
            if spec_hash != inputs.get("acceptance_config_sha256"):
                reasons.append("任务规格生成的验收配置与清单记录的不同")

    # 轮次：编号连续；同轮在 bench.jsonl 与 rounds.jsonl 中的 G、结论、镜头一致；镜头文件存在
    got = [h["round"] for h in hist]
    if got != list(range(len(got))) or got != [r["round"] for r in recorded]:
        reasons.append("轮次不连续，或 bench.jsonl 与 rounds.jsonl 的轮次不一致")
    else:
        for h, r in zip(hist, recorded):
            if float(h["spec_shortfall"]) != float(r["spec_shortfall"]) or h.get("verdict") != r.get("verdict") or h.get("file") != r.get("file"):
                reasons.append(f"第 {h['round']} 轮的 G、结论或镜头在 bench.jsonl 与 rounds.jsonl 中不一致")
            if not (job_dir / "lenses" / str(r.get("file"))).is_file():
                reasons.append(f"第 {h['round']} 轮的镜头文件缺失")
    traj = [float(h["spec_shortfall"]) for h in hist]
    passed_at = next((k for k, g in enumerate(traj) if g == 0), None)
    if not traj:
        reasons.append("没有轮次记录")
    elif traj[0] == 0:
        reasons.append("起点已通过，不是有效的恢复案例")
    elif passed_at is None and len(traj) - 1 < OBSERVE_ROUNDS:
        # 任务自身的 max_rounds 可能更小；观察不足时后续未知，不能当作"没有后续成功"
        reasons.append(f"观察不足：未通过且只观察到第 {len(traj) - 1} 轮")
    r0 = job_dir / "lenses" / "rounds" / "r000.zmx"
    start_key = hashlib.sha256(f"{task['entry']}|{frozen.get('sha256')}|{sha_file(r0)}".encode()).hexdigest()[:20]

    def round_time(h: dict[str, Any]) -> float | None:
        # 补齐恢复的轮次可能没有验收耗时：记为未知，不能当 0
        if h["round"] == 0:
            return 0.0
        if h.get("opt_s") is None or h.get("eval_s") is None:
            return None
        return float(h["opt_s"]) + float(h["eval_s"])

    return {
        "job": job_dir.name,
        "eligible": not reasons,
        "reasons": reasons,
        "trajectory": traj[: OBSERVE_ROUNDS + 1],
        "passed_at": passed_at,
        "times": [round_time(h) for h in hist],
        "interruptions": sum(1 for e in entries if e.get("event") == "interrupted"),
        "recovered_rounds": [h["round"] for h in hist if h.get("recovered_from")],
        "start_key": start_key,
    }


def collect(manifest: dict[str, Any], homes: list[Path]) -> dict[str, Any]:
    """按清单逐项准入。每个任务以最早创建的任务目录为正式运行；相同起点只计一次。

    相同起点但轨迹不同是复现冲突：两条都不进入样本，所在组在冲突解决前不能发布正式结论。"""
    jobs_by_task = find_jobs(homes, manifest["batch"])
    status: dict[str, str] = {}
    excluded, duplicates, conflicts = [], [], []
    by_start: dict[str, dict[str, Any]] = {}
    infos: dict[str, dict[str, Any]] = {}
    for task in manifest["tasks"]:
        tid = task["task"]
        dirs = jobs_by_task.get(tid, [])
        if not dirs:
            status[tid] = "missing"
            excluded.append({"task": tid, "reasons": ["没有运行"]})
            continue
        a = audit(dirs[0], manifest, task)
        info = {**{k: task[k] for k in ("task", "group", "class", "tier", "entry", "patent", "seed")}, **a, "extra_runs": [d.name for d in dirs[1:]]}
        if not a["eligible"]:
            status[tid] = "excluded"
            excluded.append({"task": tid, "job": a["job"], "reasons": a["reasons"]})
            continue
        infos[tid] = info
        first = by_start.get(a["start_key"])
        if first is None:
            by_start[a["start_key"]] = info
            status[tid] = "sample"
        elif first["trajectory"] == a["trajectory"]:
            status[tid] = "duplicate"
            duplicates.append({"task": tid, "same_start_as": first["task"]})
        else:
            status[tid] = status[first["task"]] = "conflict"
            conflicts.append({"task": tid, "same_start_as": first["task"], "trajectories": [first["trajectory"], a["trajectory"]]})
    samples = [infos[t] for t, st in status.items() if st == "sample"]
    return {"samples": samples, "status": status, "excluded": excluded, "duplicates": duplicates, "conflicts": conflicts}


def readiness(manifest: dict[str, Any], got: dict[str, Any], group: str, cls: str) -> dict[str, Any]:
    """一组一类的清单任务是否全部合格完成。缺失、被排除或有复现冲突时只能给阶段性统计。"""
    tasks = [t["task"] for t in manifest["tasks"] if t["group"] == group and t["class"] == cls]
    counts: dict[str, int] = {}
    for t in tasks:
        st = got["status"].get(t, "missing")
        counts[st] = counts.get(st, 0) + 1
    complete = bool(tasks) and set(counts) <= {"sample", "duplicate"}
    return {"group": group, "class": cls, "tasks": len(tasks), "counts": counts, "complete": complete}


# ---- 参数选择 ----


def stop_round(traj: list[float], n: int, rel: float, cap: int) -> tuple[int, str]:
    """按停机参数回放：返回停在第几轮及原因（pass / stagnated / limit）。轨迹已完整观察到 OBSERVE_ROUNDS。"""
    saved = (rounds.STAGNATION_ROUNDS, rounds.STAGNATION_REL, rounds.MAX_ROUNDS)
    rounds.STAGNATION_ROUNDS, rounds.STAGNATION_REL, rounds.MAX_ROUNDS = n, rel, cap
    try:
        for k in range(1, len(traj) + 1):
            d = rounds.decide(traj[:k])
            if d["status"] != "continue":
                return d["rounds"], d["status"]
    finally:
        rounds.STAGNATION_ROUNDS, rounds.STAGNATION_REL, rounds.MAX_ROUNDS = saved
    raise ValueError("轨迹未观察到停机或通过")


def significant(best: float, g: float, rel: float) -> bool:
    return math.isfinite(g) and (math.isinf(best) or (best - g) / best >= rel)


def evaluate(samples: list[dict[str, Any]], n: int, rel: float, cap: int) -> dict[str, Any]:
    out = {"N": n, "rel": rel, "cap": cap, "false_stops": [], "stop_rounds": 0, "time_s": 0.0, "time_unknown_rounds": 0, "missed": [], "per_sample": {}}
    for s in samples:
        traj = s["trajectory"]
        k, status = stop_round(traj, n, rel, cap)
        out["per_sample"][s["task"]] = k
        out["stop_rounds"] += k
        used = s["times"][1 : k + 1]
        out["time_s"] += sum(t for t in used if t is not None)
        out["time_unknown_rounds"] += sum(1 for t in used if t is None)
        if status != "pass" and s["passed_at"] is not None:
            out["false_stops"].append(s["task"])
        if status != "pass":
            g_stop, g_later = min(traj[: k + 1]), min(traj[k + 1 :], default=math.inf)
            if significant(g_stop, g_later, REPORT_REL):
                out["missed"].append({"task": s["task"], "stop": k, "g_at_stop": g_stop, "g_later_best": g_later})
    out["time_s"] = round(out["time_s"], 1)
    return out


def tie_key(c: dict[str, Any]) -> tuple:
    """平局规则：各参数在候选列表中的索引距初始值之和；再相同时依次取较大 N、较小 ε、较大上限。"""
    i0 = (GRID_N.index(INITIAL[0]), GRID_REL.index(INITIAL[1]), GRID_CAP.index(INITIAL[2]))
    i = (GRID_N.index(c["N"]), GRID_REL.index(c["rel"]), GRID_CAP.index(c["cap"]))
    return (sum(abs(a - b) for a, b in zip(i, i0)), -c["N"], c["rel"], -c["cap"])


def categorize(s: dict[str, Any], false_stopped_by_any: bool) -> str:
    traj = s["trajectory"]
    if s["passed_at"] is not None:
        if false_stopped_by_any:
            return "停滞后成功"
        return "快速成功" if s["passed_at"] <= QUICK_ROUNDS else "其他成功"
    best, last = traj[0], 0
    for k, g in enumerate(traj[1:], start=1):
        if significant(best, g, REPORT_REL):
            last = k
        best = min(best, g)
    if last == 0:
        return "始终无改善"
    return "持续改善但未通过" if last > OBSERVE_ROUNDS // 2 else "停滞未通过"


def coverage(samples: list[dict[str, Any]]) -> dict[str, int]:
    return {"samples": len(samples), "structures": len({s["entry"] for s in samples}), "patents": len({s["patent"] for s in samples}), "seeds": len({(s["entry"], s["seed"]) for s in samples})}


def select(samples: list[dict[str, Any]], ready: dict[str, Any]) -> dict[str, Any]:
    """ready 为 readiness() 的结果：清单任务没有全部合格完成时，只给阶段性统计。"""
    if not samples:
        return {"status": "阶段性统计：没有合格样本", "chosen": None, "readiness": ready}
    cands = [evaluate(samples, n, rel, cap) for n in GRID_N for rel in GRID_REL for cap in GRID_CAP]
    by_any = {t for c in cands for t in c["false_stops"]}
    cats: dict[str, list[dict[str, Any]]] = {}
    for s in samples:
        cats.setdefault(categorize(s, s["task"] in by_any), []).append(s)
    initial = next(c for c in cands if (c["N"], c["rel"], c["cap"]) == INITIAL)
    ok = [c for c in cands if not c["false_stops"]]
    result: dict[str, Any] = {
        "categories": {k: {**coverage(v), "tasks": [s["task"] for s in v]} for k, v in cats.items()},
        "candidates_without_false_stops": len(ok),
        "initial": {k: initial[k] for k in ("false_stops", "stop_rounds", "time_s", "time_unknown_rounds", "missed")},
        "initial_status": "被否定（调参组出现误停）" if initial["false_stops"] else "未被否定",
        "readiness": ready,
    }
    if not ok:
        result.update(status="没有合格候选" if ready["complete"] else "阶段性统计（清单任务未全部合格完成）：目前没有合格候选", chosen=None)
        return result
    best = min(c["stop_rounds"] for c in ok)
    tied = sorted((c for c in ok if c["stop_rounds"] == best), key=tie_key)
    chosen = tied[0]
    same_behaviour = [c for c in ok if c["per_sample"] == chosen["per_sample"]]
    discriminating = "停滞后成功" in cats
    result.update(
        chosen={k: chosen[k] for k in ("N", "rel", "cap", "stop_rounds", "time_s", "time_unknown_rounds", "missed")},
        tied_on_rounds=[(c["N"], c["rel"], c["cap"]) for c in tied],
        same_behaviour=[(c["N"], c["rel"], c["cap"]) for c in same_behaviour],
        status=("找到满足调参准则的候选" if discriminating else "证据不足：没有停滞后成功的轨迹，防误停能力不可辨识")
        + (f"；另有 {len(same_behaviour) - 1} 组候选在全部样本上停机轮次完全相同，具体取值不可辨识" if len(same_behaviour) > 1 else ""),
        max_pass_round=max((s["passed_at"] for s in samples if s["passed_at"] is not None), default=None),
    )
    if not ready["complete"]:
        result["status"] = "阶段性统计（清单任务未全部合格完成，不发布选择结论）：" + result["status"]
    return result


def validate(samples: list[dict[str, Any]], n: int, rel: float, cap: int, ready: dict[str, Any]) -> dict[str, Any]:
    """只有清单中该组任务全部合格完成、且没有误停时，才是"独立验证通过"。"""
    out: dict[str, Any] = {"params": (n, rel, cap), "coverage": coverage(samples), "readiness": ready}
    if not samples:
        return {**out, "passed": None, "status": "无验证结论：没有合格样本"}
    c = evaluate(samples, n, rel, cap)
    out.update({k: c[k] for k in ("false_stops", "stop_rounds", "time_s", "time_unknown_rounds", "missed")})
    if c["false_stops"]:
        # 误停在任何完成程度下都说明规则不成立
        return {**out, "passed": False, "status": "所选规则未通过验证（出现误停）"}
    if not ready["complete"]:
        return {**out, "passed": None, "status": "阶段性统计：清单任务未全部合格完成，暂无验证结论"}
    return {**out, "passed": True, "status": "独立验证通过"}
