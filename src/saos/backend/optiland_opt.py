"""Optiland 阶段的优化：变量、评价函数与 scipy 优化器（Plan 2 的有限差分路径）。

变量就是处方里的 variable 求解；变量边界、评价函数行和损失脚本存在伴随文件里，都不进结构指纹。评价函数行用 Zemax 的
操作数名，会话切到 OpticStudio 时照译成评价函数行。评价函数值的定义同 OpticStudio：sqrt(Σ w·(v − t)² / Σ w)。
内循环直接改活的 Optic（OpticalState._live_set），不经处方重建；结束时把变量写回处方，再完整同步一次（求解链、自动半口径）。
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from saos.backend.base import is_par
from saos.backend.optiland_budget import Budget, Snapshot, Stop, outcome
from saos.backend.optiland_materials import unsupported
from saos.backend.optiland_surfaces import kind
from saos.protocol import ErrorCode, SaosError

ALGORITHMS = ("trf", "lm", "lbfgsb", "nelder-mead", "powell")
TORCH_ALGORITHMS = ("torch-trf", "torch-lbfgs", "torch-adam")
ZOS_ALGORITHMS = ("dls", "od")
CYCLES = {"auto": None, "1": 1, "5": 5, "10": 10, "50": 50}
# 单面边界操作数：满足时值等于目标，不满足时为实际值，所以满足时这一行不贡献评价函数
BOUNDARY = {"CTGT": ("CTVA", 1), "CTLT": ("CTVA", -1), "ETGT": ("ETVA", 1), "ETLT": ("ETVA", -1)}
# 依赖半口径的操作数：活 Optic 上的自动半口径只在找到更好的点时重算
APERTURE_OPERANDS = {"ETVA", "ETGT", "ETLT", "SAGX", "SAGY", "SSAG"}
# 模型玻璃变量的默认范围（Abbe 近似拟合所用的常见光学玻璃大致落在这里），坐标缩放让 Vd 与 nd 同量级
GLASS_RANGE = {"nd": (1.40, 2.00, 1.0), "vd": (18.0, 95.0, 0.02)}
# 光线失败、求解失败的行在优化中按这个残差计，线搜索自然退回
PENALTY = 1e3
LIVE_KINDS = ("standard", "evenaspheric", "oddaspheric")
OPERAND_RE = re.compile(r"^[A-Z0-9]{4}$")
WIZARD_KEYS = {"type", "data", "reference", "rings", "arms", "glass", "air", "start_at", "assume_axial_symmetry"}
WIZARD_OPERANDS = {("wavefront", "centroid"): "RWCE", ("wavefront", "chiefray"): "RWCH",
                   ("spotradius", "centroid"): "RSCE", ("spotradius", "chiefray"): "RSCH"}
REPORT_EVERY_S = 1.0


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SaosError(ErrorCode.BAD_ARGS, f"{label} must be a finite number, got {value!r}")
    return float(value)


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SaosError(ErrorCode.BAD_ARGS, f"{label} must be an integer, got {value!r}")
    return value


def normalize_rows(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or not rows:
        raise SaosError(ErrorCode.BAD_ARGS, "merit add needs a non-empty JSON list of operands")
    out = []
    for n, spec in enumerate(rows, 1):
        label = f"merit row {n}"
        if not isinstance(spec, dict):
            raise SaosError(ErrorCode.BAD_ARGS, f"{label} must be an object with type, int1, int2, data, target, weight")
        extra = sorted(set(spec) - {"type", "int1", "int2", "data", "target", "weight"})
        if extra:
            raise SaosError(ErrorCode.BAD_ARGS, f"{label}: unknown keys {extra}")
        name = str(spec.get("type", "")).upper()
        if not OPERAND_RE.match(name):
            raise SaosError(ErrorCode.BAD_ARGS, f"{label}: type must be a 4-character Zemax operand name, got {spec.get('type')!r}")
        data = spec.get("data", [])
        if not isinstance(data, list) or len(data) > 6:
            raise SaosError(ErrorCode.BAD_ARGS, f"{label}: data must be a list of at most 6 numbers")
        weight = _number(spec.get("weight", 1.0), f"{label} weight")
        if weight < 0:
            raise unsupported(f"{label}: a negative weight (an OpticStudio Lagrangian constraint) has no Optiland counterpart")
        out.append({"type": name, "int1": _integer(spec.get("int1", 0), f"{label} int1"), "int2": _integer(spec.get("int2", 0), f"{label} int2"),
                    "data": [_number(v, f"{label} data") for v in data], "target": _number(spec.get("target", 0.0), f"{label} target"),
                    "weight": weight})
    return out


def row_value(b, row: dict[str, Any]) -> float:
    from saos.backend import optiland_operands

    name = row["type"]
    if name in BOUNDARY:
        base, sign = BOUNDARY[name]
        raw = optiland_operands.operand(b, base, row["int1"], 0, [0.0, 0.0] if base == "ETVA" else [])
        target = row["target"]
        return target if sign * (raw - target) >= 0 else raw
    return optiland_operands.operand(b, name, row["int1"], row["int2"], row["data"])


def evaluate(b, rows: list[dict[str, Any]]) -> tuple[list[float], list[str | None]]:
    """逐行取值；光线失败等计算错误记在 errors 里（值为 nan），未映射的操作数直接报出。"""
    values, errors = [], []
    for row in rows:
        try:
            value = float(row_value(b, row))
            error = None if math.isfinite(value) else "the value is not finite (a ray failed or missed)"
        except SaosError as exc:
            if exc.code == ErrorCode.BACKEND_UNSUPPORTED:
                raise
            value, error = math.nan, exc.message
        values.append(value)
        errors.append(error)
    return values, errors


def merit(rows: list[dict[str, Any]], values: list[float], script_value: float | None = None, script_weight: float = 1.0) -> float:
    total, weights = 0.0, 0.0
    for row, value in zip(rows, values):
        if row["weight"] == 0:
            continue
        total += row["weight"] * ((value - row["target"]) ** 2 if math.isfinite(value) else math.inf)
        weights += row["weight"]
    if script_value is not None and script_weight > 0:
        total += script_weight * (script_value ** 2 if math.isfinite(script_value) else math.inf)
        weights += script_weight
    return math.sqrt(total / weights) if weights > 0 else 0.0


def wizard_rows(b, settings: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """OpticStudio 序列优化向导的对应：每个视场一行多波长 RMS（高斯求积，视场权重为行权重），加逐面的中心与边缘厚度边界。
    arms 只校验不起作用：Optiland 的 RMS 操作数固定按 12 条臂积分。返回 (行, start_at)。"""
    if not isinstance(settings, dict):
        raise SaosError(ErrorCode.BAD_ARGS, "optimization wizard settings must be a JSON object")
    extra = sorted(set(settings) - WIZARD_KEYS)
    if extra:
        raise SaosError(ErrorCode.BAD_ARGS, f"optimization wizard settings {extra} unknown; allowed {sorted(WIZARD_KEYS)}")

    def name(key: str, default: str) -> str:
        value = settings.get(key, default)
        if not isinstance(value, str):
            raise SaosError(ErrorCode.BAD_ARGS, f"optimization wizard {key} must be a name, got {value!r}")
        return value.replace(" ", "").lower()

    def integer(key: str, default: int, low: int, high: int) -> int:
        value = settings.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise SaosError(ErrorCode.BAD_ARGS, f"optimization wizard {key} must be an integer {low}–{high}, got {value!r}")
        return value

    if name("type", "RMS") != "rms":
        raise unsupported("the Optiland optimization wizard builds RMS merit functions only")
    data, reference = name("data", "Wavefront"), name("reference", "Centroid")
    if (data, reference) not in WIZARD_OPERANDS:
        raise unsupported(f"the Optiland optimization wizard maps data Wavefront or Spot Radius with reference Centroid or Chief Ray, "
                          f"not {settings.get('data')!r} / {settings.get('reference')!r}")
    rings, arms, start_at = integer("rings", 3, 1, 20), settings.get("arms", 6), integer("start_at", 1, 1, 10 ** 6)
    if arms not in (6, 8, 10):
        raise SaosError(ErrorCode.BAD_ARGS, f"optimization wizard arms must be 6, 8 or 10, got {arms!r}")
    if not isinstance(settings.get("assume_axial_symmetry", True), bool):
        raise SaosError(ErrorCode.BAD_ARGS, "optimization wizard assume_axial_symmetry must be true or false")
    limits = {}
    for key in ("glass", "air"):
        value = settings.get(key)
        if value is not None and not (isinstance(value, list) and len(value) == 3
                                      and all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in value)):
            raise SaosError(ErrorCode.BAD_ARGS, f"optimization wizard {key} must be [minimum, maximum, edge] numbers, got {value!r}")
        limits[key] = value
    operand = WIZARD_OPERANDS[(data, reference)]
    rows = [{"type": operand, "int1": rings, "int2": 0, "data": [float(hx), float(hy)], "target": 0.0, "weight": float(w)}
            for (hx, hy), w in zip(b._normalized_fields(), b._effective["fields"]["weights"]) if w > 0]
    for i in range(1, len(b._rows) - 1):
        row = b._rows[i]
        material = str(row.get("material", "") or "").upper()
        if kind(row) == "coordinatebreak" or material == "MIRROR":
            continue
        limit = limits["air" if material in ("", "AIR") else "glass"]
        if limit:
            lo, hi, edge = (float(x) for x in limit)
            rows += [{"type": "CTGT", "int1": i, "int2": 0, "data": [], "target": lo, "weight": 1.0},
                     {"type": "CTLT", "int1": i, "int2": 0, "data": [], "target": hi, "weight": 1.0},
                     {"type": "ETGT", "int1": i, "int2": 0, "data": [], "target": edge, "weight": 1.0}]
    return rows, start_at


def variables(b) -> list[tuple[int, str]]:
    p = b._require()
    rows = [p["object"], *p["surfaces"], p.get("image") or {}]
    out = [(i, column) for i, row in enumerate(rows) for column, spec in row.get("solves", {}).items() if spec.get("type") == "variable"]
    # 模型玻璃变量只在材料仍是模型玻璃时有效（之后换成牌号的面不再算）
    return out + [(i, column) for i in sorted(b._glass_vary) if isinstance(b._rows[i].get("material"), dict) for column in ("nd", "vd")]


def script_scalar(script: Callable[[], Any]) -> float:
    """脚本损失必须是一个数：多元素向量或张量明确拒绝，不让 float() 抛出未处理的 ValueError。"""
    try:
        value = script()
    except SaosError:
        raise
    except Exception as exc:
        raise SaosError(ErrorCode.SCRIPT_ERROR, f"merit script raised {type(exc).__name__}: {exc}") from exc
    size = value.numel() if hasattr(value, "numel") else 1
    if size != 1:
        raise SaosError(ErrorCode.SCRIPT_ERROR, f"the merit loss script must return one number, got {size} values")
    try:
        value = float(value)
    except (ValueError, TypeError) as exc:
        raise SaosError(ErrorCode.SCRIPT_ERROR, "the merit loss script must return one number") from exc
    if not math.isfinite(value):
        raise SaosError(ErrorCode.SCRIPT_ERROR, "the merit loss script returned a non-finite value")
    return value


class Problem:
    """变量在优化器里的坐标：半径列取归一化边缘曲率 c·R（R 为该面净半口径，平面为 0），偶次 / 奇次非球面系数取边缘矢高贡献
    A·R^(2N) / A·R^N，厚度按系统最大半口径归一，其余原值。边界换算到同一坐标。"""

    def __init__(self, b, rows: list[dict[str, Any]], script: Callable[[], Any] | None, script_weight: float):
        self.b, self.rows, self.script, self.script_weight = b, rows, script, script_weight
        self.keys = variables(b)
        if not self.keys:
            raise SaosError(ErrorCode.BAD_ARGS, "the lens has no variables; make parameters vary with saos opt vary first")
        self.live = b._live_capable(self.keys)
        self.apertures = self.live and any(row["type"] in APERTURE_OPERANDS for row in rows)
        effective = b._rows
        length = max([abs(float(r.get("semi_diameter", 0.0) or 0.0)) for r in effective[1:-1]] + [1.0])
        self.unit, lo, hi, x0 = [], [], [], []
        self.clipped = []
        for i, column in self.keys:
            row = effective[i]
            semi = abs(float(row.get("semi_diameter", 0.0) or 0.0)) or length
            nominal = b._nominal(i, column)
            bound = b._bounds.get(f"{i}.{column}") or {}
            low, high = bound.get("min"), bound.get("max")
            if column == "radius":
                unit, value = semi, 0.0 if nominal == 0 or math.isinf(nominal) else 1.0 / nominal
                if bound.get("param") == "radius" and low is not None:
                    low, high = sorted((1.0 / low, 1.0 / high))
            elif column == "thickness":
                unit, value = 1.0 / length, nominal
            elif column in GLASS_RANGE:
                low, high, unit = GLASS_RANGE[column]
                value = nominal
            elif is_par(column) and kind(row) in ("evenaspheric", "oddaspheric"):
                n = int(column[3:])
                unit, value = semi ** (2 * n if kind(row) == "evenaspheric" else n), nominal
            else:
                unit, value = 1.0, nominal
            self.unit.append(unit)
            lo.append(-np.inf if low is None else low * unit)
            hi.append(np.inf if high is None else high * unit)
            x = value * unit
            if not lo[-1] <= x <= hi[-1]:
                self.clipped.append(f"{i}.{column}")
                x = min(max(x, lo[-1]), hi[-1])
            x0.append(x)
        self.lo, self.hi, self.x0 = np.array(lo), np.array(hi), np.array(x0, dtype=float)
        self.bounded = bool(np.isfinite(self.lo).any() or np.isfinite(self.hi).any())

    def physical(self, x: np.ndarray) -> dict[tuple[int, str], float]:
        out = {}
        for (i, column), unit, value in zip(self.keys, self.unit, x):
            value = float(value) / unit
            out[(i, column)] = (math.inf if value == 0 else 1.0 / value) if column == "radius" else value
        return out

    def apply(self, x: np.ndarray) -> None:
        values = self.physical(x)
        if self.live:
            for (i, column), value in values.items():
                self.b._live_set(i, column, value)
            self.b._live_settle()
            self.b._invalidate()
        else:
            self.b._commit(values)

    def measure(self) -> tuple[list[float], list[str | None], float | None]:
        values, errors = evaluate(self.b, self.rows)
        script_value = None
        if self.script is not None:
            try:
                script_value = script_scalar(self.script)
            except SaosError as exc:
                if exc.code in (ErrorCode.BACKEND_UNSUPPORTED, ErrorCode.SCRIPT_ERROR):
                    raise
                script_value = math.nan
        return values, errors, script_value

    def residuals(self, x: np.ndarray) -> tuple[np.ndarray, float]:
        weights = [row["weight"] for row in self.rows] + ([self.script_weight] if self.script is not None else [])
        total = sum(weights) or 1.0
        scale = np.sqrt(np.asarray(weights, dtype=float) / total)
        if not np.isfinite(x).all() or np.any(x < self.lo) or np.any(x > self.hi):
            return scale * PENALTY, PENALTY
        try:
            self.apply(x)
            values, _, script_value = self.measure()
        except SaosError as exc:
            if exc.code in (ErrorCode.BACKEND_UNSUPPORTED, ErrorCode.SCRIPT_ERROR):
                raise
            return scale * PENALTY, PENALTY
        diff = [v - row["target"] for v, row in zip(values, self.rows)] + ([script_value] if self.script is not None else [])
        diff = np.array([d if math.isfinite(d) else PENALTY for d in diff], dtype=float)
        r = scale * diff
        return r, float(np.sqrt(np.dot(r, r)))


def optimize(b, *, algorithm: str | None, cycles: str, max_seconds: float | None, on_progress, should_cancel,
             script: Callable[[], Any] | None = None, script_weight: float = 1.0, device: str | None = None) -> dict[str, Any]:
    from saos.backend.optiland_torch import TorchUnavailable

    requested = algorithm or "auto"
    selected = "torch-trf" if requested == "auto" else requested
    budget = Budget(max_seconds, should_cancel)
    budget.watch(script)
    original = Snapshot(b)
    fallback = None
    result = None
    b._opt_budget = budget
    try:
        with budget.interruptible():
            try:
                result = _optimize(b, algorithm=selected, cycles=cycles, max_seconds=budget.remaining(),
                                   on_progress=on_progress, should_cancel=should_cancel, script=script,
                                   script_weight=script_weight, device=device)
            except TorchUnavailable as exc:
                if requested != "auto":
                    raise
                fallback = {"from": selected, "to": "trf", "reason": exc.reason, "message": exc.message}
                original.restore(b)
                budget.best = None
                budget.result.update(initial_merit=None, final_merit=None, evaluations=0, accepted_steps=0, rejected_steps=0)
                budget.check()
                selected = "trf"
                result = _optimize(b, algorithm=selected, cycles=cycles, max_seconds=budget.remaining(),
                                   on_progress=on_progress, should_cancel=should_cancel, script=script,
                                   script_weight=script_weight, device="cpu")
            budget.result.update(result)
    except Stop:
        result = {**budget.result, "stopped_by": budget.stopped_by,
                  "message": "cancelled" if budget.stopped_by == "cancel" else "max_seconds reached"}
    finally:
        glass_budget = getattr(b, "_glass_budget", None)
        if glass_budget is not None:
            # 外层计时器可能在局部调用返回前中断；算法与已验证快照必须先发布给外层。
            choice = {"algorithm": selected, "requested_algorithm": requested, "fallback": fallback}
            choices = glass_budget.result.setdefault("local_algorithms", [])
            if choice not in choices:
                choices.append(choice)
            if glass_budget.result["initial_merit"] is None:
                glass_budget.result["initial_merit"] = budget.result.get("initial_merit")
            value = budget.result.get("final_merit")
            previous = glass_budget.result.get("final_merit")
            if result is not None and budget.best is not None and value is not None and (previous is None or value < previous):
                glass_budget.best = budget.best
                glass_budget.result["final_merit"] = value
        # 快照来自已完整重建且通过关卡的 NumPy Optic；退出不再做求解或光线计算。
        (budget.best or original).restore(b)
        del b._opt_budget
    for key in ("initial_merit", "final_merit"):
        if result.get(key) is not None and not math.isfinite(result[key]):
            result[key] = None
    result.update(outcome(result.get("initial_merit"), result.get("final_merit"),
                          converged=result.get("converged", False), stopped_by=result.get("stopped_by"),
                          admissible=budget.best is not None))
    result.update(algorithm=selected, requested_algorithm=requested, fallback=fallback,
                  budget_s=max_seconds, elapsed_s=round(time.monotonic() - budget.started, 3),
                  restored=budget.best is None, admissible_result=budget.best is not None)
    result.setdefault("device", "cuda" if device == "cuda" and selected in TORCH_ALGORITHMS else "cpu")
    return result


def _optimize(b, *, algorithm: str, cycles: str, max_seconds: float | None, on_progress, should_cancel,
              script: Callable[[], Any] | None = None, script_weight: float = 1.0, device: str | None = None) -> dict[str, Any]:
    from scipy.optimize import least_squares, minimize
    from saos.backend.optiland_guard import MAX_BACKTRACKS, MIN_VALID_FRACTION, StepGuard
    b._opt_budget.watch(least_squares)
    b._opt_budget.watch(minimize)

    if algorithm in ZOS_ALGORITHMS:
        raise unsupported(f"--algorithm {algorithm} is OpticStudio's optimiser")
    if algorithm in TORCH_ALGORITHMS:
        from saos.backend import optiland_torch

        if device not in (None, "auto", "cpu", "cuda"):
            raise SaosError(ErrorCode.BAD_ARGS, f"--device must be auto, cpu or cuda, got {device!r}")
        return optiland_torch.optimize(b, algorithm=algorithm, cycles=cycles, max_seconds=max_seconds, on_progress=on_progress,
                                       should_cancel=should_cancel, script=script, script_weight=script_weight, device=device)
    if algorithm not in ALGORITHMS:
        raise SaosError(ErrorCode.BAD_ARGS, f"algorithm must be one of {list(ALGORITHMS + TORCH_ALGORITHMS)} (or dls / od, which run in OpticStudio)")
    if device not in (None, "auto", "cpu"):
        raise SaosError(ErrorCode.BAD_ARGS, f"--device {device} applies to the torch algorithms; {algorithm} runs on the CPU")
    if cycles not in CYCLES:
        raise SaosError(ErrorCode.BAD_ARGS, f"cycles must be one of {list(CYCLES)}")
    rows = list(b._merit)
    if not rows and script is None:
        raise SaosError(ErrorCode.BAD_ARGS, "the merit function is empty; use saos opt merit wizard, add or script first")
    b._sync_optical_state()
    problem = Problem(b, rows, script, script_weight)
    n = len(problem.keys)
    budget = b._opt_budget
    if algorithm == "lm" and problem.bounded:
        raise SaosError(ErrorCode.BAD_ARGS, "lm takes no variable bounds; use trf, lbfgsb, nelder-mead or powell, or remove --min / --max")
    if algorithm == "lm" and len(rows) + (script is not None) < n:
        raise SaosError(ErrorCode.BAD_ARGS, f"lm needs at least as many merit rows as variables ({n}); use trf")
    started = time.monotonic()
    # 起点求值：未映射的操作数、脚本错误在这里报出（混合后端据前者把会话切到 OpticStudio）
    try:
        problem.apply(problem.x0)
        values, errors, script_value = problem.measure()
    except BaseException:
        # 外层用进入时的镜头快照恢复，避免取消后重新做长追迹。
        raise
    initial = merit(rows, values, script_value, script_weight)
    budget.result.update(initial_merit=initial if math.isfinite(initial) else None,
                         final_merit=initial if math.isfinite(initial) else None, variables=n,
                         targets=len(rows) + (script is not None), inner_loop="live" if problem.live else "rebuild", device="cpu")
    state: dict[str, Any] = {"best": (math.inf, problem.x0.copy()), "evaluations": 0, "stopped_by": None, "reported": started}
    guard = StepGuard(problem)
    if math.isfinite(initial) and guard.admissible(problem.x0):
        state["best"] = (initial, problem.x0.copy())
        budget.best = Snapshot(guard.probe)

    def residuals(x: np.ndarray) -> np.ndarray:
        budget.check()
        x = np.asarray(x, dtype=float)
        r, value = problem.residuals(x)
        state["evaluations"] += 1
        budget.result["evaluations"] = state["evaluations"]
        admissible = value < PENALTY and guard.admissible(x)
        if not admissible:
            # 求解器必须收到原试探点的拒绝值；不能把回退点的残差冒充原点残差，否则雅可比会对应错误坐标。
            anchor = state["best"][1]
            for attempt in range(1, MAX_BACKTRACKS + 1):
                budget.check()
                trial = anchor + (x - anchor) * 2.0**(-attempt)
                if not np.isfinite(trial).all():
                    break
                guard.backtracks += 1
                _, trial_value = problem.residuals(trial)
                state["evaluations"] += 1
                if trial_value < PENALTY and guard.admissible(trial):
                    guard.accept_apertures()
                    if trial_value < state["best"][0]:
                        snapshot = Snapshot(guard.probe)
                        state["best"] = (trial_value, trial.copy())
                        budget.best = snapshot
                        budget.result["final_merit"] = trial_value
                    break
            r, value = np.full_like(r, PENALTY), PENALTY
        if admissible and value < state["best"][0]:
            guard.accept_apertures()
            snapshot = Snapshot(guard.probe)
            state["best"] = (value, np.array(x, dtype=float))
            budget.best = snapshot
            budget.result["final_merit"] = value
            if problem.apertures:
                try:
                    b._update_apertures()
                except SaosError:
                    pass
        now = time.monotonic()
        if on_progress is not None and now - state["reported"] >= REPORT_EVERY_S:
            state["reported"] = now
            on_progress({"merit": state["best"][0], "evaluations": state["evaluations"], "elapsed_s": round(now - started, 2)})
        return r

    def squared(x: np.ndarray) -> float:
        r = residuals(x)
        return float(np.dot(r, r))

    limit = CYCLES[cycles]
    bounds = list(zip(problem.lo, problem.hi))
    message, success = "", False
    try:
        if algorithm in ("trf", "lm"):
            # trf 不把数值雅可比的求值计入 max_nfev；lm 计入，所以按一个“周期” = 一次雅可比加一步换算
            nfev = None if limit is None else limit if algorithm == "trf" else limit * (n + 1)
            result = least_squares(residuals, problem.x0, method=algorithm, max_nfev=nfev,
                                   bounds=(problem.lo, problem.hi) if algorithm == "trf" else (-np.inf, np.inf))
        else:
            method = {"lbfgsb": "L-BFGS-B", "nelder-mead": "Nelder-Mead", "powell": "Powell"}[algorithm]
            result = minimize(squared, problem.x0, method=method, bounds=bounds if problem.bounded else None,
                              options={} if limit is None else {"maxiter": limit})
        message = str(result.message)
        success = bool(result.success and np.allclose(result.x, state["best"][1], rtol=1e-7, atol=1e-9))
    finally:
        guard.close()
    final = budget.result["final_merit"]
    budget.result["converged"] = success
    out = {"initial_merit": initial, "final_merit": final, "variables": n, "targets": len(rows) + (script is not None),
           **outcome(initial, final, converged=success, stopped_by=None, admissible=budget.best is not None), "stopped_by": None,
           "elapsed_s": round(time.monotonic() - started, 3), "algorithm": algorithm, "evaluations": state["evaluations"],
           "inner_loop": "live" if problem.live else "rebuild", "message": message,
           "backtracks": guard.backtracks, "step_guard_reason": guard.last_reason,
           "min_valid_fraction": MIN_VALID_FRACTION, "max_backtracks": MAX_BACKTRACKS,
           "admissible_result": math.isfinite(state["best"][0])}
    if problem.clipped:
        out["start_clipped_to_bounds"] = problem.clipped
    return out


def glass_search(b, *, max_seconds=None, should_cancel=None, **kwargs):
    budget = Budget(max_seconds, should_cancel)
    budget.watch(kwargs.get("script"))
    original = Snapshot(b)
    b._glass_budget = budget
    try:
        with budget.interruptible():
            result = _glass_search(b, max_seconds=budget.remaining(), should_cancel=should_cancel, **kwargs)
    except Stop:
        result = {**budget.result, "stopped_by": budget.stopped_by, "message": "glass search interrupted"}
    finally:
        (budget.best or original).restore(b)
        del b._glass_budget
    for key in ("initial_merit", "final_merit"):
        if result.get(key) is not None and not math.isfinite(result[key]):
            result[key] = None
    result.update(outcome(result.get("initial_merit"), result.get("final_merit"),
                          converged=result.get("stopped_by") is None, stopped_by=result.get("stopped_by"),
                          admissible=budget.best is not None))
    result.update(budget_s=max_seconds, elapsed_s=round(time.monotonic() - budget.started, 3), restored=budget.best is None)
    return result


def _glass_search(b, *, surfaces: list[int] | None, catalog: str, neighbours: int, cycles: str, max_seconds: float | None,
                 on_progress, should_cancel, script: Callable[[], Any] | None = None, script_weight: float = 1.0,
                 algorithm: str | None = None) -> dict[str, Any]:
    """Optiland 阶段的在产玻璃替换：沿用 Optiland GlassExpert 的贪心邻域算法（先在整个目录的 k 均值代表里找，再在当前玻璃的
    最近邻里找；每试一种玻璃都对连续变量做一次局部优化，变好才保留），候选牌号与色散全部取自在产 AGF（MaterialStore），
    不用 GlassExpert 按名字查 refractiveindex 数据库的部分。AGF 色散公式 1、2 以外的牌号不作候选，列在 skipped 里。"""
    from scipy.cluster.vq import kmeans2

    budget = b._glass_budget
    started = budget.started
    if isinstance(neighbours, bool) or not isinstance(neighbours, int) or not 2 <= neighbours <= 50:
        raise SaosError(ErrorCode.BAD_ARGS, f"--neighbours must be an integer 2–50, got {neighbours!r}")
    b._sync_optical_state()
    rows = b._rows

    def glassy(i: int) -> bool:
        material = rows[i].get("material", "")
        return bool(material) and not (isinstance(material, str) and material.upper() in ("AIR", "MIRROR"))

    surfaces = surfaces if surfaces is not None else [i for i in range(1, len(rows) - 1) if glassy(i)]
    bad = [i for i in surfaces if not (0 < i < len(rows) - 1) or not glassy(i)]
    if bad or not surfaces:
        raise SaosError(ErrorCode.BAD_ARGS, f"surfaces {bad} hold no glass" if bad else "the lens has no glass to substitute")
    varying = sorted(set(surfaces) & set(b._glass_vary))
    if varying:
        raise SaosError(ErrorCode.BAD_ARGS, f"surfaces {varying} have a varying model glass; fix it first "
                                            "(saos opt vary --param material --off) or leave them out of --surfaces")
    records = b._store.catalog(catalog)
    if not records:
        raise SaosError(ErrorCode.BAD_ARGS, f"catalog {catalog} has no local AGF file")
    pool, skipped = {}, []
    for name, record in records.items():
        if record["formula"] in (1, 2) and len(record["coefficients"]) >= 6:
            pool[name] = (float(record["glass"].nd), float(record["glass"].vd))
        else:
            skipped.append(name)
    if len(pool) < 2:
        raise SaosError(ErrorCode.BAD_ARGS, f"catalog {catalog} has fewer than 2 glasses with AGF formula 1 or 2")
    names = list(pool)
    points = np.array([pool[n] for n in names])
    # nd 与 Vd 按目录内的离散程度归一后再算距离；GlassExpert 用原值，距离几乎只看 Vd
    spread = np.where(points.std(axis=0) > 0, points.std(axis=0), 1.0)
    unit = points / spread
    if catalog.upper() not in [c.upper() for c in b._require()["catalogs"]]:
        b._change(lambda p: p["catalogs"].append(catalog.upper()))
    has_variables = bool(variables(b))
    state: dict[str, Any] = {"trials": 0, "stopped_by": None}
    local_algorithms = []
    budget.result["local_algorithms"] = local_algorithms

    def remaining() -> float | None:
        return budget.remaining()

    def check_stop() -> bool:
        budget.check()
        return state["stopped_by"] is not None

    def settle() -> float:
        """当前玻璃下的评价函数：有连续变量时先局部优化，否则直接取值。"""
        if has_variables:
            seconds = remaining()
            if check_stop() or seconds == 0:
                state["stopped_by"] = state["stopped_by"] or "timeout"
                return math.inf
            result = optimize(b, algorithm=algorithm, cycles=cycles, max_seconds=seconds, on_progress=None,
                              should_cancel=should_cancel, script=script, script_weight=script_weight)
            choice = {k: result.get(k) for k in ("algorithm", "requested_algorithm", "fallback")}
            if choice not in local_algorithms:
                local_algorithms.append(choice)
            if result.get("stopped_by"):
                state["stopped_by"] = state["stopped_by"] or result["stopped_by"]
            if budget.result["initial_merit"] is None:
                budget.result["initial_merit"] = result.get("initial_merit")
            return result["final_merit"] if result.get("final_merit") is not None else math.inf
        values, _ = evaluate(b, b._merit)
        script_value = script_scalar(script) if script is not None else None
        return merit(b._merit, values, script_value, script_weight)

    best = settle()
    if not math.isfinite(best) and not state["stopped_by"]:
        raise SaosError(ErrorCode.OPTILAND_ERROR, "initial glass-search merit is non-finite")
    initial = budget.result["initial_merit"] if has_variables else best
    budget.result.update(initial_merit=initial, final_merit=best if math.isfinite(best) else initial)
    if math.isfinite(best):
        budget.best = Snapshot(b)
    before = {i: b.surface_value(i, "material") for i in surfaces}

    def explore(surface: int, candidates: list[str]) -> None:
        nonlocal best
        saved = Snapshot(b)
        chosen_state = None
        for glass in candidates:
            if check_stop():
                break
            if glass == b.surface_value(surface, "material"):
                continue
            state["trials"] += 1
            budget.result["trials"] = state["trials"]
            try:
                b._change(lambda p: b._nominal_row(p, surface).__setitem__("material", glass))
                value = settle()
            except SaosError as exc:
                if exc.code in (ErrorCode.BACKEND_UNSUPPORTED, ErrorCode.SCRIPT_ERROR):
                    raise
                value = math.inf
            else:
                if value < best:
                    best, chosen_state = value, Snapshot(b)
                    budget.best = chosen_state
                    budget.result["final_merit"] = best
            finally:
                saved.restore(b)
            if on_progress is not None:
                on_progress({"merit": best, "surface": surface, "candidate": glass, "trials": state["trials"],
                             "elapsed_s": round(time.monotonic() - started, 2)})
        if chosen_state is not None:
            chosen_state.restore(b)

    # 第一遍：目录的 k 均值代表（固定种子，结果可复现）
    _, labels = kmeans2(unit, min(neighbours, len(names)), minit="points", seed=1234)
    representatives = []
    for label in sorted(set(labels.tolist())):
        members = np.flatnonzero(labels == label)
        centre = unit[members].mean(axis=0)
        representatives.append(names[members[np.argmin(np.linalg.norm(unit[members] - centre, axis=1))]])
    for surface in surfaces:
        if check_stop():
            break
        explore(surface, representatives)
    # 第二遍：当前玻璃在 (nd, Vd) 上最近的 neighbours 种（多取一种，当前玻璃自己会被跳过）
    for surface in surfaces:
        if check_stop():
            break
        here = np.array([float(b.surface_value(surface, "nd")), float(b.surface_value(surface, "vd"))]) / spread
        order = np.argsort(np.linalg.norm(unit - here, axis=1))
        explore(surface, [names[k] for k in order[:neighbours + 1]])
    after = {i: b.surface_value(i, "material") for i in surfaces}
    out = {"initial_merit": initial, "final_merit": best, "catalog": catalog.upper(), "surfaces": surfaces,
           "changes": [{"surface": i, "before": before[i], "after": after[i]} for i in surfaces if before[i] != after[i]],
           "trials": state["trials"], "candidates": len(pool), "stopped_by": state["stopped_by"],
           "elapsed_s": round(time.monotonic() - started, 3), "reoptimized": has_variables, "local_algorithms": local_algorithms}
    if skipped:
        out["skipped"] = {"reason": "AGF dispersion formula other than 1 or 2", "glasses": sorted(skipped)}
    return out


GLOBAL_METHODS = ("de", "basinhopping", "shgo")


def global_search(b, *, method: str | None, max_seconds: float, on_progress, should_cancel, folder, keep: int = 10,
                  script: Callable[[], Any] | None = None, script_weight: float = 1.0) -> dict[str, Any]:
    """在预算内验证并保存候选；到期后只恢复已验证快照。"""
    from scipy.optimize import basinhopping, differential_evolution, shgo
    from saos.backend.optiland_guard import StepGuard

    budget = Budget(max_seconds, should_cancel)
    for fn in (script, basinhopping, differential_evolution, shgo):
        budget.watch(fn)
    original = Snapshot(b)
    method = method or "de"
    if method not in GLOBAL_METHODS:
        raise SaosError(ErrorCode.BAD_ARGS, f"--method must be one of {list(GLOBAL_METHODS)}")
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise SaosError(ErrorCode.BAD_ARGS, "keep must be a positive integer")
    rows = list(b._merit)
    if not rows and script is None:
        raise SaosError(ErrorCode.BAD_ARGS, "the merit function is empty")
    chosen, saved, unsaved = {}, {}, []
    guard = None
    converged = False
    initial = None
    file = b.file
    message = ""
    try:
        with budget.interruptible():
            b._sync_optical_state()
            problem = Problem(b, rows, script, script_weight)
            if method in ("de", "shgo") and not (np.isfinite(problem.lo).all() and np.isfinite(problem.hi).all()):
                raise SaosError(ErrorCode.BAD_ARGS, "global de/shgo require finite bounds on every variable")
            guard = StepGuard(problem)
            reported = time.monotonic()

            def objective(x):
                nonlocal reported
                budget.check()
                point = np.asarray(x, dtype=float)
                _, value = problem.residuals(point)
                budget.result["evaluations"] += 1
                unique = not any(np.max(np.abs(point - item[1])) <= 1e-6 for item in chosen.values())
                slot = next((i for i in range(keep) if i not in chosen), None)
                if slot is None:
                    slot = max(chosen, key=lambda i: chosen[i][0])
                eligible = slot not in chosen or value < chosen[slot][0]
                if unique and eligible and value < PENALTY and guard.admissible(point):
                    snapshot = Snapshot(guard.probe)
                    chosen[slot] = (value, point.copy(), snapshot)
                    best_value, _, best_snapshot = min(chosen.values(), key=lambda item: item[0])
                    budget.best = best_snapshot
                    budget.result["final_merit"] = best_value
                    budget.check()
                    path = folder / f"GLOPT_{slot:04d}.zmx"
                    try:
                        # 写已验证 probe 的快照；不把主优化器改成候选，也不在到期后重建。
                        guard.probe.save_as(path)
                        saved[slot] = value
                    except SaosError as exc:
                        unsaved.append({"file": path.name, "error": exc.message})
                now = time.monotonic()
                if on_progress is not None and now - reported >= REPORT_EVERY_S:
                    reported = now
                    on_progress({"merit": budget.result["final_merit"], "systems": budget.result["evaluations"],
                                 "elapsed_s": now - budget.started})
                return value * value

            initial = math.sqrt(objective(problem.x0))
            budget.result["initial_merit"] = initial
            bounds = list(zip(problem.lo, problem.hi))
            if method == "de":
                result = differential_evolution(objective, bounds, x0=problem.x0, seed=1234, polish=False, maxiter=100000, tol=1e-8)
            elif method == "shgo":
                result = shgo(objective, bounds, options={"maxtime": budget.remaining()})
            else:
                local = {"method": "L-BFGS-B", "bounds": bounds if problem.bounded else None}
                result = basinhopping(objective, problem.x0, niter=100000, minimizer_kwargs=local,
                                      stepsize=0.5, niter_success=50, seed=1234)
            message = str(getattr(result, "message", "") or "")
            converged = bool(getattr(result, "success", False))
    except Stop:
        message = "cancelled" if budget.stopped_by == "cancel" else "max_seconds reached"
    finally:
        if guard is not None:
            guard.close()
        (budget.best or original).restore(b)
        b.file = file
    final = budget.result["final_merit"] if budget.best else initial
    out = {"initial_merit": initial, "final_merit": final, "systems": budget.result["evaluations"],
           **outcome(initial, final, converged=converged, stopped_by=budget.stopped_by, admissible=budget.best is not None),
           "stopped_by": budget.stopped_by, "elapsed_s": round(time.monotonic() - budget.started, 3),
           "budget_s": max_seconds, "method": method, "message": message,
           "best_merits": sorted(saved.values()), "saved": [f"GLOPT_{i:04d}.zmx" for i in sorted(saved)],
           "restored": budget.best is None, "admissible_result": budget.best is not None}
    if unsaved:
        out["unsaved"] = unsaved
    return out
