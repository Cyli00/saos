"""优化基准的驱动（DESIGN.md 实现顺序第 5 步）。

对一个基准案例：搭建收录结构 → 受控扰动 → 第 0 轮验收 → 按参考策略逐轮优化并验收，
记录每轮的规格差距，写入任务目录的 bench.jsonl。

这里不调用 round.record，也不在中途按停滞规则停机：校准要看到完整的轨迹，
停滞阈值和轮次上限由 replay() 在记录好的轨迹上离线回放比较。

参考策略（reference-v1）模拟一个按固定顺序放开变量、再针对未通过项加权的 agent：
  第 1 轮   变量：物镜各面曲率 + 工作距离间隔；评价函数：RMS 波前（质心，6 环 6 臂）+ 焦距 + 结构界限
            （工作距离、玻璃中心厚和边缘厚、空气间隔、光学长度、通光直径）
  第 2 轮   再放开物镜内部的全部厚度
  第 3 轮起 对当前最好一轮中未通过的项加入针对性操作数并加权：第一次针对即 ×3，此后每轮再 ×3，最多 ×3⁵
  每轮都从此前规格差距最小的一轮出发；某轮变差时，下一轮回到最好的那一轮。

v1 没有约束通光直径：US11048071B2-e1（seed 2，×8 扰动）第 1 轮后只剩通光直径差 0.0135，
此后针对性加权无处可加，19 轮结果完全相同。v2 补上 MXSD 边界操作数。
v2 第一次针对时权重不放大，结构界限项（工作距离、长度等）本来就在基础评价函数里，
于是第 3 轮与第 2 轮完全相同，白白多出一个无进展轮次（US11048071B2-e1 拉伸规格）。v3 改为第一次针对即放大。
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

import yaml

from zea import client, config, jobs, library, prescription, rounds
from zea.protocol import ErrorCode, ZeaError, jsonable
from zea.spec import Spec, load_spec

POLICY = "reference-v3"
MARECHAL = 1 / 14
ESCALATION = 3.0
MAX_ESCALATION_STEPS = 5
# 受控扰动要让起点"明确不满足规格"：G0 ≥ 1，即至少相当于一个单项超出一整个允许量。
# 低 NA 结构对默认幅度不敏感（实测 US12493165B2-e3、e4 在 ±1% / ±3% 下 G0 = 0），所以按倍数逐级放大
START_G_MIN = 1.0
AMPLIFY = (1, 2, 4, 8)


def clearly_failing(g0: float) -> bool:
    return g0 >= START_G_MIN


def _now() -> float:
    return time.perf_counter()


class Case:
    def __init__(self, job: jobs.Job, entry_id: str, timeout: float, cores: int | None = None):
        self.cores = cores
        self.job = job
        self.entry_id = entry_id
        self.timeout = timeout
        self.spec_name = "spec/benchmark.yaml"
        self.spec: Spec = load_spec(job.root / self.spec_name)
        self.log_path = job.root / "bench.jsonl"

    def req(self, op: str, **args: Any) -> Any:
        return client.request(op, {"job": self.job.id, **args}, timeout=self.timeout)

    # ---- 布局 ----

    def layout(self) -> dict[str, Any]:
        rows = self.req("lens.surfaces")["surfaces"]
        first, last = prescription.objective_block(rows)
        block = list(range(first, last + 1))
        lens = [r["index"] for r in rows[first : last + 1] if r["type"] == "Standard" and r["comment"] != prescription.TAG_STOP]
        return {
            "rows": rows,
            "block": block,
            "gap": prescription.gap_surface(rows),
            "first": lens[0],
            "last": lens[-1],
            "radii": [i for i in lens if math.isfinite(rows[i]["radius"])],
            "thickness": list(range(lens[0], lens[-1])),
        }

    def limit(self, item_id: str, default: float) -> float:
        item = self.spec.item(item_id)
        return item.threshold if item else default

    # ---- 评价函数 ----

    def merit_rows(self, lay: dict[str, Any], failing: dict[str, int]) -> list[dict[str, Any]]:
        """按从上到下的顺序给出附加操作数；failing 为各未通过项已连续未通过的轮数。"""
        s = self.spec
        first, last, gap = lay["first"], lay["last"], lay["gap"]

        def w(item_id: str, base: float = 10.0) -> float:
            steps = min(failing[item_id] + 1, MAX_ESCALATION_STEPS) if item_id in failing else 0
            return base * ESCALATION**steps

        sag = float(self.req("lens.operand", type="SAGY", int1=first)["value"])
        wd_min = self.limit("working_distance", 0.0)
        rows: list[dict[str, Any]] = [
            {"type": "EFLY", "int1": first, "int2": last, "target": s.objective_focal_length, "weight": w("magnification")},
            {"type": "CTGT", "int1": gap, "target": wd_min * 1.02 - min(0.0, sag), "weight": w("working_distance")},
            {"type": "MNCG", "int1": first, "int2": last, "target": self.limit("center_thickness_glass", 0.5) + 0.02, "weight": w("center_thickness_glass")},
            {"type": "MNEG", "int1": first, "int2": last, "data": [0, 1], "target": self.limit("edge_thickness_glass", 0.3) + 0.02, "weight": w("edge_thickness_glass")},
            {"type": "MNCA", "int1": first, "int2": last - 1, "target": max(self.limit("air_gap", 0.0), 0.0) + 0.02, "weight": w("air_gap")},
            {"type": "MNEA", "int1": first, "int2": last - 1, "data": [0, 1], "target": 0.02, "weight": w("air_gap")},
            {"type": "TTHI", "int1": 0, "int2": last - 1, "weight": 0.0},
        ]
        # OPLT 引用上一行（TTHI）的行号；行号在 rows 写完后统一填
        rows.append({"type": "OPLT", "ref": len(rows) - 1, "target": self.limit("optical_length", 1e3) * 0.99, "weight": w("optical_length")})
        # 通光直径：验收取物镜部分（含虚拟光阑面）各面半口径最大值的 2 倍。MXSD 是边界操作数，
        # 各面都不超过目标时返回目标值，否则返回目标值加超出量（实测目标为 0 时返回各面半口径之和）
        rows.append({"type": "MXSD", "int1": lay["block"][0], "int2": lay["block"][-1], "target": self.limit("clear_diameter", 1e3) / 2 * 0.99, "weight": w("clear_diameter")})

        m2 = s.magnification**2
        if "axial_color" in failing:
            scale = self.limit("axial_color", 3.4) * 1e-3 * m2  # 物方 µm → 像方 mm（纵向放大率 M²）
            rows.append({"type": "AXCL", "int1": 1, "int2": 3, "target": 0.0, "weight": w("axial_color", 1.0) * (MARECHAL / scale) ** 2})
        if "lateral_color" in failing:
            scale = self.limit("lateral_color", 16.7) * 1e-3
            rows.append({"type": "LACL", "int1": 1, "int2": 3, "target": 0.0, "weight": w("lateral_color", 1.0) * (MARECHAL / scale) ** 2})
        if "osc" in failing:
            for zone in (0.7, 1.0):
                rows.append({"type": "OSCD", "int1": 0, "int2": 2, "data": [zone], "target": 0.0, "weight": w("osc", 1.0) * (MARECHAL / 0.0025) ** 2})
        if "plan_field" in failing or "field_rms_wfe" in failing:
            # 平场判据的离焦界限折算成 Zernike 离焦系数（波），作为广义场曲操作数的尺度
            na_img = s.na / s.magnification
            scale = s.berek_image_um * na_img**2 / (4 * math.sqrt(3) * 0.546)
            steps = min(max(failing.get("plan_field", -1), failing.get("field_rms_wfe", -1)) + 1, MAX_ESCALATION_STEPS)
            for op in ("FCGS", "FCGT"):
                for hy in (0.7, 1.0):
                    rows.append({"type": op, "int1": 2, "data": [0.0, hy], "target": 0.0, "weight": ESCALATION**steps * (MARECHAL / scale) ** 2})
        return rows

    def apply_merit(self, lay: dict[str, Any], failing: dict[str, int]) -> list[dict[str, Any]]:
        rows = self.merit_rows(lay, failing)
        # merit add 把每行插到第 1 行，所以倒序提交；第 k 个（从 0 起）最终位于第 k+1 行
        for r in rows:
            if "ref" in r:
                r["int1"] = r.pop("ref") + 1
            if "ref2" in r:
                r["int2"] = r.pop("ref2") + 1
        self.req("opt.merit", action="clear")
        self.req("opt.merit", action="wizard", settings={"type": "RMS", "data": "Wavefront", "reference": "Centroid", "rings": 6, "arms": 6, "start_at": 1})
        self.req("opt.merit", action="add", rows=list(reversed(rows)))
        return rows

    # ---- 轮次 ----

    def runtime(self) -> dict[str, Any]:
        """紧接在 round.record 之前查询后台进程的运行模式和代码版本：后台进程可能在两轮之间被重启。"""
        info = self.req("ping")
        return {"mode": info.get("mode"), "daemon_code": info.get("code_sha256")}

    def evaluate(self, note: str, runtime: dict[str, Any]) -> dict[str, Any]:
        """用 round.record（observe）记录一个轮次：照常冻结并核对验收口径，停机判断只记录不执行。"""
        out = self.req("round.record", spec=self.spec_name, note=note, observe=True)
        rec = out["record"]
        return {
            **runtime,
            "round": rec["round"],
            "file": rec["file"],
            "verdict": rec["verdict"],
            # 无法评估时后台进程把 ∞ 序列化为字符串 "inf"
            "spec_shortfall": float(rec["spec_shortfall"]),
            "items": rec["items"],
            "invalid": out["evaluation"].get("invalid"),
            "decision": out["decision"],
        }

    def record(self, rec: dict[str, Any]) -> None:
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(jsonable(rec), ensure_ascii=False, default=str) + "\n")

    def start(self, seed: int, radius_rel: float, thickness_rel: float, amplify: bool = True) -> dict[str, Any]:
        """第 0 轮：从 start.zmx 施加受控扰动，幅度按 AMPLIFY 放大到起点明确不满足规格为止。

        放大过程中的试探只做验收评估、不记入轮次；选定的起点再由 round.record 记为第 0 轮并冻结验收口径。"""
        started = _now()
        factors = AMPLIFY if amplify else (1,)
        attempts = []
        for factor in factors:
            self.req("lens.load", file="start.zmx")
            self.req("lens.perturb", seed=seed, radius_rel=radius_rel * factor, thickness_rel=thickness_rel * factor)
            if len(factors) == 1:
                break
            self.req("lens.save", file=f"start-x{factor}.zmx", overwrite=True)
            g = float(self.req("eval.accept", spec=self.spec_name)["spec_shortfall"])
            attempts.append({"factor": factor, "spec_shortfall": g})
            if clearly_failing(g):
                break
        perturbation = {"seed": seed, "radius_rel": radius_rel * factor, "thickness_rel": thickness_rel * factor, "factor": factor, "attempts": attempts}
        r0 = {"policy": POLICY, "strategy": "受控扰动后的初始状态", "perturbation": perturbation, **self.evaluate("round 0", self.runtime())}
        r0["elapsed_s"] = round(_now() - started, 1)
        self.record(r0)
        return r0

    def reconcile(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """续跑前核对 bench.jsonl 与 rounds.jsonl。

        每轮先写一条 pending（策略、优化结果），round.record 完成后再写正式记录。
        如果在 round.record 完成后、写正式记录前中断，rounds.jsonl 会多出这一轮：
        用 pending 和 rounds.jsonl 的记录把它补齐。其他不一致一律拒绝续跑。"""
        other = sorted({str(h.get("policy")) for h in history} - {POLICY})
        if other:
            raise ZeaError(ErrorCode.BAD_ARGS, f"轨迹中有其他版本参考策略的轮次（{other}），不能用 {POLICY} 续跑", {"job": self.job.id})
        recorded = rounds.load(self.job.root / "rounds.jsonl")
        got, want = [h["round"] for h in history], [r["round"] for r in recorded]
        if want == got:
            return history
        pending = last_event(self.log_path, "pending")
        if want == got + [len(got)] and pending and pending["round"] == len(got):
            r = recorded[-1]
            rec = {
                **{k: v for k, v in pending.items() if k != "event"},
                "round": r["round"],
                "file": r["file"],
                "verdict": r["verdict"],
                "spec_shortfall": float(r["spec_shortfall"]),
                "items": r["items"],
                "recovered_from": "rounds.jsonl",
            }
            self.record(rec)
            return [*history, rec]
        raise ZeaError(
            ErrorCode.BAD_ARGS,
            "rounds.jsonl 与 bench.jsonl 的轮次不一致，不能续跑",
            {"rounds": want, "bench": got, "pending": pending and pending.get("round")},
        )

    def run(self, max_rounds: int) -> list[dict[str, Any]]:
        history = load_history(self.log_path)
        if not history:
            raise ValueError("先用 start() 记录第 0 轮")
        history = self.reconcile(history)
        if history[0]["spec_shortfall"] == 0:
            return history
        # 续跑时从最后一轮恢复加权进度，否则同一历史下的权重会与连续运行不同
        last = history[-1].get("strategy")
        streak: dict[str, int] = dict(last.get("targeted") or {}) if isinstance(last, dict) else {}
        while history[-1]["spec_shortfall"] != 0 and len(history) <= max_rounds:
            number = len(history)
            try:
                rec, streak = self.one_round(history, streak, number)
            except ZeaError as exc:
                # 超时、后台进程断开等：记为实验中断，不是光学上的不通过或停滞；可用 --job 续跑
                self.record({"event": "interrupted", "round": number, "policy": POLICY, "error": exc.code, "message": exc.message})
                raise
            history.append(rec)
        return history

    def one_round(self, history: list[dict[str, Any]], streak: dict[str, int], number: int) -> tuple[dict[str, Any], dict[str, int]]:
        best = min(history, key=lambda h: h["spec_shortfall"])
        started = _now()
        # 每轮都显式加载所选的最好快照，不依赖后台进程里当前是哪个镜头：
        # 续跑补齐中断前已记录的一轮后，后台进程里可能仍是更早的镜头
        loaded = self.req("lens.load", file=best["file"])["file"]
        streak = next_streak(streak, best["items"], number)
        lay = self.layout()
        self.req("opt.vary", param="radius", surfaces=lay["radii"], on=True)
        self.req("opt.vary", param="thickness", surfaces=[lay["gap"]], on=True)
        if number >= 2:
            self.req("opt.vary", param="thickness", surfaces=lay["thickness"], on=True)
        rows = self.apply_merit(lay, streak)
        t_opt = _now()
        opt = self.req("opt.local", cycles="auto", **({"cores": self.cores} if self.cores else {}))
        pending = {
            "event": "pending",
            "round": number,
            "policy": POLICY,
            "from": loaded,
            "strategy": {
                "variables": "radii+wd" + ("+thickness" if number >= 2 else ""),
                "targeted": streak,
                "operands": [r["type"] for r in rows],
            },
            "merit": {"initial": opt["initial_merit"], "final": opt["final_merit"]},
            "opt_s": round(_now() - t_opt, 1),
            # 中断后补齐这一轮时，用的就是这里保存的运行信息，而不是续跑时的后台进程
            **self.runtime(),
        }
        self.record(pending)
        t_eval = _now()
        rec = {**{k: v for k, v in pending.items() if k != "event"}, **self.evaluate(f"round {number}", {k: pending[k] for k in ("mode", "daemon_code")})}
        rec["eval_s"] = round(_now() - t_eval, 1)
        rec["elapsed_s"] = round(_now() - started, 1)
        self.record(rec)
        return rec, streak


def next_streak(streak: dict[str, int], failing: dict[str, Any], number: int) -> dict[str, int]:
    """针对性加权从第 3 轮开始；值为该项此前已被针对的轮数，决定权重放大几次。"""
    return {k: streak.get(k, -1) + 1 for k in failing} if number >= 3 else {}


STRETCH_KEYS = {"wd": ("working_distance_min", "wd_mech_mm"), "length": ("optical_length_max", "optical_length_mm")}


def stretched_spec(spec: dict[str, Any], metrics: dict[str, Any], stretch: dict[str, float]) -> dict[str, Any]:
    """拉伸规格：把结构界限设为原结构实测值的给定倍数，模拟库中结构离用户规格还有距离的任务。"""
    out = json.loads(json.dumps(spec))
    for key, factor in stretch.items():
        if key not in STRETCH_KEYS:
            raise ValueError(f"只能拉伸 {list(STRETCH_KEYS)}，收到 {key}")
        limit, metric = STRETCH_KEYS[key]
        out["limits"][limit] = round(float(metrics[metric]) * factor, 3)
    return out


def prepare(
    entry_id: str,
    seed: int,
    radius_rel: float,
    thickness_rel: float,
    timeout: float,
    amplify: bool = True,
    stretch: dict[str, float] | None = None,
    discard: bool = False,
    formal: dict[str, Any] | None = None,
) -> jobs.Job:
    """formal：正式批次任务的标识（batch、task、group、class），写进任务状态供准入检查。"""
    d = library.entry_dir(entry_id)
    tag = "".join(f"-{k}{round(v * 100)}" for k, v in sorted((stretch or {}).items()))
    job = jobs.create_job(f"bench-{entry_id}-s{seed}-r{round(radius_rel * 1000)}{tag}")
    spec = yaml.safe_load((d / "benchmark.yaml").read_text(encoding="utf-8"))
    if stretch:
        spec = stretched_spec(spec, library.load_entry(entry_id)["metrics"], stretch)
    (job.root / "spec" / "benchmark.yaml").write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    # 处方副本：准入时与实验清单记录的输入哈希核对
    (job.root / "sources" / "prescription.json").write_bytes((d / "prescription.json").read_bytes())
    job.update_state(
        spec="spec/benchmark.yaml",
        bench={
            "entry": entry_id,
            "seed": seed,
            "radius_rel": radius_rel,
            "thickness_rel": thickness_rel,
            "stretch": stretch or {},
            "policy": POLICY,
            "client_code": config.code_sha256(),
            **(formal or {}),
        },
    )
    args = {"job": job.id}
    client.request("lens.build", {**args, "prescription": library.read_json(d / "prescription.json"), "file": "start.zmx", "overwrite": True, "discard": discard}, timeout=timeout)
    Case(job, entry_id, timeout).start(seed, radius_rel, thickness_rel, amplify)
    return job


def run_case(
    entry_id: str,
    seed: int,
    radius_rel: float,
    thickness_rel: float,
    max_rounds: int,
    timeout: float,
    job_id: str | None = None,
    amplify: bool = True,
    stretch: dict[str, float] | None = None,
    cores: int | None = None,
    discard: bool = False,
    formal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """discard 只作用于开始时后台进程里已加载的镜头：有未保存修改时默认拒绝（UNSAVED_CHANGES）。"""
    if job_id:
        job = jobs.get_job(job_id)
        history = load_history(job.root / "bench.jsonl")
        if history:
            # 后台进程里留着的是本基准任务中断时的未保存镜头时，这一轮会整轮重做，可以放弃；
            # 其他任务的未保存镜头仍按 discard 处理
            loaded = (client.request("ping", {}, timeout=timeout) or {}).get("loaded") or {}
            own = loaded.get("job") == job.id
            client.request("lens.load", {"job": job.id, "file": history[-1]["file"], "discard": discard or own}, timeout=timeout)
    else:
        job = prepare(entry_id, seed, radius_rel, thickness_rel, timeout, amplify, stretch, discard, formal)
    state = job.read_state()
    job.update_state(bench={**state["bench"], "max_rounds": max_rounds})
    history = Case(job, entry_id, timeout, cores).run(max_rounds)
    return {
        "job": job.id,
        "entry": entry_id,
        "perturbation": history[0].get("perturbation"),
        "trajectory": [h["spec_shortfall"] for h in history],
        "final": history[-1]["verdict"],
    }


# ---- 离线回放 ----


def replay(trajectory: list[float], stagnation_rounds: int, stagnation_rel: float, max_rounds: int) -> dict[str, Any]:
    """按给定停机参数回放一条完整轨迹：返回停在第几轮、停机原因、停机时的最好规格差距。"""
    saved = (rounds.STAGNATION_ROUNDS, rounds.STAGNATION_REL, rounds.MAX_ROUNDS)
    rounds.STAGNATION_ROUNDS, rounds.STAGNATION_REL, rounds.MAX_ROUNDS = stagnation_rounds, stagnation_rel, max_rounds
    try:
        for k in range(1, len(trajectory) + 1):
            d = rounds.decide(trajectory[:k])
            if d["status"] != "continue":
                return {"stop_round": d["rounds"], "status": d["status"], "g_best": d["g_best"]}
        return {"stop_round": None, "status": "unfinished", "g_best": min(trajectory)}
    finally:
        rounds.STAGNATION_ROUNDS, rounds.STAGNATION_REL, rounds.MAX_ROUNDS = saved


def first_pass(trajectory: list[float]) -> int | None:
    return next((k for k, g in enumerate(trajectory) if g == 0), None)


def load_history(path: Path) -> list[dict[str, Any]]:
    """bench.jsonl 中的正式轮次记录（跳过 pending 和 interrupted 事件）。"""
    history = [h for h in rounds.load(path) if "event" not in h]
    for h in history:
        h["spec_shortfall"] = float(h["spec_shortfall"])
    return history


def last_event(path: Path, kind: str) -> dict[str, Any] | None:
    events = [h for h in rounds.load(path) if h.get("event") == kind]
    return events[-1] if events else None


def completeness(job_dir: Path, bench_state: dict[str, Any]) -> str:
    """complete：通过，或走满 max_rounds；interrupted：最后一条事件是实验中断；否则 incomplete。"""
    path = Path(job_dir) / "bench.jsonl"
    history = load_history(path)
    entries = rounds.load(path)
    if not history:
        return "incomplete"
    if history[-1]["spec_shortfall"] == 0 or len(history) - 1 >= int(bench_state.get("max_rounds", 10**9)):
        return "complete"
    return "interrupted" if entries and entries[-1].get("event") == "interrupted" else "incomplete"


def load_trajectory(job_dir: Path) -> list[float]:
    return [h["spec_shortfall"] for h in load_history(Path(job_dir) / "bench.jsonl")]


def write_summary(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def calibrate(trajectories: dict[str, list[float]], grid_rounds: tuple[int, ...], grid_rel: tuple[float, ...], grid_max: tuple[int, ...]) -> list[dict[str, Any]]:
    """在完整轨迹上比较各组停机参数。只用停机时已经看到的轮次计算代价，不与停机后的轨迹相减。

    rounds_used：各案例停机（或通过）时的轮次之和；轨迹没走到停机的按其长度计。
    false_stops：停机时还没通过、但完整轨迹之后通过了的案例。
    idle_rounds：未通过而停机的案例，停机轮次减去停机前最后一次明显进步的轮次（≥ 0）。
    missed：停机之后最好规格差距又缩小至少 stagnation_rel 的案例（含 false_stops），记停机时与之后的最好值。
    """
    rows = []
    for n in grid_rounds:
        for rel in grid_rel:
            for cap in grid_max:
                out = {"stagnation_rounds": n, "stagnation_rel": rel, "max_rounds": cap, "passed": 0, "false_stops": [], "rounds_used": 0, "idle_rounds": 0, "missed": []}
                for name, traj in trajectories.items():
                    r = replay(traj, n, rel, cap)
                    stop = r["stop_round"] if r["stop_round"] is not None else len(traj) - 1
                    out["rounds_used"] += stop
                    if r["status"] == "pass":
                        out["passed"] += 1
                        continue
                    seen, after = traj[: stop + 1], traj[stop + 1 :]
                    if first_pass(after) is not None:
                        out["false_stops"].append(name)
                    out["idle_rounds"] += stop - last_progress(seen, rel)
                    g_stop, g_later = min(seen), min(after, default=math.inf)
                    if g_later < g_stop and (math.isinf(g_stop) or (g_stop - g_later) / g_stop >= rel):
                        out["missed"].append({"case": name, "stop_round": stop, "g_at_stop": g_stop, "g_later": g_later})
                rows.append(out)
    return rows


def last_progress(trajectory: list[float], rel: float) -> int:
    """规格差距最后一次相对此前最好值缩小至少 rel 的轮次（没有则为 0）。"""
    best, last = trajectory[0], 0
    for k, g in enumerate(trajectory[1:], start=1):
        if math.isfinite(g) and (math.isinf(best) or (best - g) / best >= rel):
            last = k
        best = min(best, g)
    return last


def bench_jobs(homes: list[Path] | None = None) -> dict[Path, dict[str, Any]]:
    """各 ZEA_HOME 下的全部基准任务：任务目录 → 扰动与条目信息。并行运行时每个后台进程有自己的 ZEA_HOME。"""
    out = {}
    for home in homes or [config.zea_home()]:
        for state_file in sorted((Path(home) / "jobs").glob("*/job.json")):
            state = json.loads(state_file.read_text(encoding="utf-8"))
            if state.get("bench") and (state_file.parent / "bench.jsonl").is_file():
                out[state_file.parent] = state["bench"]
    return out


def run_task(batch: str, task_id: str, timeout: float, cores: int | None = None, discard: bool = False) -> dict[str, Any]:
    """按实验清单运行一个正式任务；本 ZEA_HOME 中已有该任务时续跑最早的那个。

    运行前核对：客户端与后台进程的代码版本都等于清单记录的版本，后台进程为清单要求的模式。"""
    from zea import calibration

    manifest = calibration.load_plan(batch)
    task = calibration.task_of(manifest, task_id)
    if manifest["policy"] != POLICY:
        raise ZeaError(ErrorCode.BAD_ARGS, f"清单要求策略 {manifest['policy']}，当前代码是 {POLICY}")
    info = client.request("ping", {}, timeout=timeout)
    mismatch = {
        "client_code": config.code_sha256() != manifest["code_sha256"],
        "daemon_code": info.get("code_sha256") != manifest["code_sha256"],
        "mode": info.get("mode") != manifest["mode"],
    }
    mismatch["inputs"] = calibration.task_inputs(task["entry"], task["stretch"]) != task.get("inputs")
    if any(mismatch.values()):
        raise ZeaError(ErrorCode.BAD_ARGS, "运行环境或实验输入与实验清单不一致，不能作为正式任务运行", {k: v for k, v in mismatch.items() if v})
    existing = calibration.find_jobs([config.zea_home()], batch).get(task_id, [])
    formal = {"batch": batch, "task": task_id, "group": task["group"], "class": task["class"]}
    return run_case(
        task["entry"],
        task["seed"],
        task["radius_rel"],
        task["thickness_rel"],
        manifest["observe_rounds"],
        timeout,
        existing[0].name if existing else None,
        task["amplify"],
        task["stretch"],
        cores,
        discard,
        formal,
    )
