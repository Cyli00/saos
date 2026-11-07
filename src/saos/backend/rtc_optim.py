"""rtc 实例的优化器：带精确约束的阻尼最小二乘，以及在它之上的 Hammer 与全局搜索（ADR-0017）。
Optimisers of the rtc instance: damped least squares with exact constraints, and Hammer and global search on top of it.

局部优化（学 CODE V AUT 的做法）：评价函数里的目标行（非边界类、权重 > 0）组成残差 r = √W·(V − T)；边界类行
（CTGT、MNCG、OPGT…，权重 > 0）不进残差，而是不等式约束 g ≥ 0，每步在线性化的约束下用有效集与拉格朗日乘子求解
    min ½‖J·d + r‖² + ½μ·dᵀD·d   s.t.  g_A + G_A·d = 0（A 为有效约束）,
D 为 JᵀJ 的对角（Marquardt 缩放），μ 为阻尼。约束因此被精确满足，而不是像罚项那样以一点违背换残差下降。范围类边界行
（MNCT 等）按面拆成各自的约束。雅可比为前向差分。接受准则：约束违背不增加时残差平方和下降，或约束违背明显减小。
Local optimisation (after CODE V's AUT): target rows form the residual; boundary rows become inequality constraints
held exactly through an active set with Lagrange multipliers in the linearised step. Forward-difference Jacobian.

Hammer：反复扰动当前最好解（有换料变量时同时随机换一面玻璃为目录里 n、V 最近的几种之一），再局部优化，更好就留下。
全局搜索：从大幅扰动的多个起点各自局部优化，保留评价函数最小的 10 个。两者都只比较满足全部约束的解，直到时间用完或取消。
Hammer perturbs the best solution (swapping one glass to a near neighbour when material substitution is on) and
re-optimises; global search optimises from many strongly perturbed starts and keeps the 10 best. Only solutions meeting
every constraint compete; both run until the budget ends or a cancel.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from saos.backend import rtc_merit
from saos.budget import Budget, Stop, checkpoint_interval, stalled
from saos.protocol import ErrorCode, SaosError

# 约束违背在这个相对量以内算满足 / a constraint violated by less than this (relative) counts as met
FEASIBLE_TOL = 1e-9
# 模型玻璃变量的取值范围（玻璃图的宽边界，不是某家目录的边界）/ bounds of model-glass variables (a wide glass-map box)
MODEL_BOUNDS = {"nd": (1.35, 2.2), "vd": (15.0, 100.0)}
# 每次迭代里阻尼最多加大的次数 / at most this many damping increases per iteration
DAMPING_TRIES = 12
SEARCH_KEEP = 10


@dataclass
class Point:
    x: np.ndarray
    r: np.ndarray
    g: np.ndarray
    scale: np.ndarray
    merit: float
    labels: list[str] = field(default_factory=list)

    @property
    def phi(self) -> float:
        return float(self.r @ self.r)

    @property
    def violation(self) -> float:
        """约束的最大相对违背；无约束时为 0。/ The largest relative constraint violation; 0 without constraints."""
        if not self.g.size:
            return 0.0
        return float(np.max(np.maximum(0.0, -self.g) / self.scale))

    @property
    def violation_norm(self) -> float:
        """所有违反约束的相对违背二范数；无约束时为 0。"""
        if not self.g.size:
            return 0.0
        return float(np.linalg.norm(np.maximum(0.0, -self.g) / self.scale))

    @property
    def feasible(self) -> bool:
        return self.violation <= FEASIBLE_TOL


class Problem:
    """变量 + 评价函数行 + 后端：给出某组变量值下的残差、约束与评价函数值。
    Variables, merit rows and the backend: residuals, constraints and the merit value at given variable values."""

    def __init__(self, backend, rows: list[rtc_merit.Row], keys: list[str]):
        self.backend = backend
        self.rows = rows
        self.keys = list(keys)
        self.evaluator = rtc_merit.Evaluator(backend, rows)
        self.budget: Budget | None = None
        self.evaluations = 0
        self.steps = np.array([variable_step(backend, k) for k in self.keys])
        self.spans = np.array([variable_span(backend, k) for k in self.keys])
        self.bounds = [MODEL_BOUNDS.get(k.split(".", 1)[1]) for k in self.keys]

    def values(self) -> np.ndarray:
        return np.array([float(self.backend.get_param(k)) for k in self.keys])

    def apply(self, x: np.ndarray) -> None:
        self.backend.apply_values(dict(zip(self.keys, (float(v) for v in x))))

    def measure(self, x: np.ndarray) -> Point | None:
        """在 x 处求值；有正权重的行算不出（光线追不过等）时返回 None。/ Evaluate at x; None when a weighted row fails."""
        if self.budget is not None:
            self.budget.check()
        try:
            self.apply(x)
        except SaosError as exc:
            if exc.code in (ErrorCode.BAD_ARGS, ErrorCode.VALUE_NOT_APPLIED):
                return None
            raise
        self.evaluations += 1
        result = self.evaluator.evaluate()
        if not math.isfinite(result.merit):
            return None
        r, g, scale, labels = [], [], [], []
        for k, row in enumerate(self.rows):
            if row.type in rtc_merit.INERT or row.type == "CONF" or row.weight <= 0:
                continue
            if row.type in rtc_merit.BOUNDARY:
                direction = rtc_merit.BOUNDARY[row.type][1]
                for label, actual in result.actual.get(k, []):
                    g.append(actual - row.target if direction == ">" else row.target - actual)
                    scale.append(max(abs(row.target), 1e-3))
                    labels.append(f"row {k + 1} {row.type} {label}")
                continue
            r.append(math.sqrt(row.weight) * (result.values[k] - row.target))
        for (lo_hi, value) in zip(self.bounds, x):
            if lo_hi is not None:
                lo, hi = lo_hi
                g += [value - lo, hi - value]
                scale += [max(abs(lo), 1e-3), max(abs(hi), 1e-3)]
                labels += ["model glass lower bound", "model glass upper bound"]
        return Point(np.array(x, dtype=float), np.array(r, dtype=float), np.array(g, dtype=float), np.array(scale, dtype=float),
                     result.merit, labels)

    def jacobian(self, base: Point) -> tuple[np.ndarray, np.ndarray]:
        """前向差分；某一步追迹失败时改向后差分，两边都失败的列为 0。/ Forward differences; backward when a step fails."""
        J = np.zeros((base.r.size, len(self.keys)))
        G = np.zeros((base.g.size, len(self.keys)))
        try:
            for j, h in enumerate(self.steps):
                for sign in (1.0, -1.0):
                    x = base.x.copy()
                    x[j] += sign * h
                    p = self.measure(x)
                    if p is not None and p.r.size == base.r.size and p.g.size == base.g.size:
                        J[:, j] = (p.r - base.r) / (sign * h)
                        G[:, j] = (p.g - base.g) / (sign * h)
                        break
        finally:
            self.apply(base.x)
        return J, G


def variable_target(backend, key: str) -> tuple[int, str]:
    """变量键对应的 (面号, 面参数)；多重结构的格（config.<c>.<r>）按它那一行的操作数。"""
    if key.startswith("config."):
        return backend.config_cell_target(key)
    surface, param = key.split(".", 1)
    return int(surface), param


def variable_step(backend, key: str) -> float:
    """差分步长：让矢高或折射率的变化在 1e-7 量级。/ A difference step that moves sag or index by about 1e-7."""
    surface, param = variable_target(backend, key)
    value = float(backend.get_param(key))
    h = semi(backend, surface)
    if param == "curvature":
        return max(1e-7 * abs(value), 2e-7 / (h * h))
    if param in ("thickness", "semi_diameter"):
        return max(1e-7 * abs(value), 1e-7)
    if param == "conic":
        return max(1e-7 * abs(value), 1e-7)
    if param.startswith("par"):
        n = int(param[3:])
        if backend.lens.surface(surface).type.upper() == "EVENASPH":
            return 1e-7 / h ** (2 * n)
        return max(1e-7 * abs(value), 1e-9)
    if param == "nd":
        return 1e-7
    if param == "vd":
        return 1e-5
    raise SaosError(ErrorCode.BAD_ARGS, f"no difference step for variable {key}")


def variable_span(backend, key: str) -> float:
    """Hammer 扰动的典型幅度：矢高变化约 2% 口径、厚度约 10%。/ The typical Hammer perturbation of a variable."""
    surface, param = variable_target(backend, key)
    value = float(backend.get_param(key))
    h = semi(backend, surface)
    if param == "curvature":
        return 0.04 / (h * h)
    if param == "thickness":
        return 0.1 * max(abs(value), 0.5)
    if param == "conic":
        return 0.2 * (abs(value) + 0.5)
    if param.startswith("par"):
        n = int(param[3:])
        if backend.lens.surface(surface).type.upper() == "EVENASPH":
            return 1e-3 * h / h ** (2 * n)
        return 0.1 * max(abs(value), 1e-6)
    return {"nd": 0.03, "vd": 4.0}.get(param, 0.1 * max(abs(value), 1e-3))


def semi(backend, surface: int) -> float:
    """面的半口径；没有口径时取系统里最大的那个，再没有取 1 mm。/ A surface's semi-diameter, with fallbacks."""
    lens = backend.lens
    h = lens.surface(surface).semi_diameter
    if h <= 0:
        h = max((s.semi_diameter for s in lens.surfaces[1:]), default=0.0)
    return h if h > 0 else 1.0


def _kkt_step(H: np.ndarray, grad: np.ndarray, G: np.ndarray, g: np.ndarray, scale: np.ndarray,
              active: set[int], restore: float = 1.0) -> tuple[np.ndarray, set[int], np.ndarray]:
    """有效集法求一步：返回 (d, 最终有效集, 乘子)。最多 3p+5 次增删。已违反的有效约束只要求线性化后恢复 restore 这一比例
    （g + G·d = (1 − restore)·g），其余有效约束要求 g + G·d = 0。
    The active-set step: (d, active set, multipliers). A violated active constraint is asked to recover the fraction
    restore of its violation; the other active constraints are met to first order."""
    p, n = G.shape
    active = set(active)
    tol = FEASIBLE_TOL
    lam = np.zeros(0)
    d = np.zeros(n)
    for _ in range(3 * p + 5):
        idx = sorted(active)
        A = G[idx] if idx else np.zeros((0, n))
        K = np.block([[H, -A.T], [A, np.zeros((len(idx), len(idx)))]])
        target = -g[idx] * np.where(g[idx] < -tol * scale[idx], restore, 1.0)
        rhs = np.concatenate([-grad, target])
        sol, *_ = np.linalg.lstsq(K, rhs, rcond=None)
        d, lam = sol[:n], sol[n:]
        # 满足且乘子为负的约束不该再顶着：去掉最负的一个 / a met constraint with a negative multiplier is released
        release = [(lam[i], c) for i, c in enumerate(idx) if lam[i] < -1e-12 and g[c] > -tol * scale[c]]
        if release:
            active.discard(min(release)[1])
            continue
        # 线性化后会被违背的约束加进来：加违背最多的一个 / a constraint the linear step would break joins the active set
        lin = (g + G @ d) / scale
        candidates = [(lin[c], c) for c in range(p) if c not in active and lin[c] < -tol]
        if candidates:
            active.add(min(candidates)[1])
            continue
        break
    return d, active, lam


def local(problem: Problem, *, iterations: int | None = None, deadline: float | None = None,
          should_cancel: Callable[[], bool] | None = None, on_progress: Callable[[dict[str, Any]], None] | None = None,
          start: np.ndarray | None = None) -> dict[str, Any]:
    """带精确约束的阻尼最小二乘。iterations 为 None 时跑到收敛；deadline 为 time.monotonic() 的截止时刻。
    结束时后端里是找到的最好解。Damped least squares with exact constraints; the backend holds the best point at the end."""
    started = time.monotonic()
    x0 = problem.values() if start is None else np.asarray(start, dtype=float)
    point = problem.measure(x0)
    if point is None:
        problem.apply(x0)
        raise SaosError(ErrorCode.BAD_ARGS, "the merit function cannot be computed at the starting point (a row has no value, e.g. a ray "
                        "misses or is totally internally reflected); diagnose the start before optimising",
                        {"rows_failed": _failed_rows(problem)})
    initial = point
    mu = 1e-3
    stopped_by = None
    converged = False
    calm = 0
    it = 0
    active: set[int] = set(np.flatnonzero(point.g <= FEASIBLE_TOL * point.scale)) if point.g.size else set()
    budget = Budget(None, should_cancel)
    budget.seconds = None if deadline is None else deadline - budget.started
    problem.budget = budget
    try:
        while iterations is None or it < iterations:
            budget.check()
            it += 1
            J, G = problem.jacobian(point)
            H = J.T @ J
            grad = J.T @ point.r
            diag = np.diag(H).copy()
            floor = 1e-12 * (float(diag.max()) if diag.size and diag.max() > 0 else 1.0)
            D = np.diag(np.maximum(diag, floor))
            accepted = None
            for _ in range(DAMPING_TRIES):
                # 违反约束的起点：恢复量随阻尼收缩，否则恢复部分不受阻尼影响、整步越过非线性区后被拒（ar-glasses 复盘）
                # from a violated start the restoration shrinks with the damping; a forced full jump was rejected every time
                restore = 1.0 if point.feasible else 1.0 / (1.0 + mu)
                d, active_try, _ = _kkt_step(H + mu * D, grad, G, point.g, point.scale, active, restore)
                trial = problem.measure(point.x + d)
                if trial is not None and _better(trial, point):
                    accepted, active = trial, active_try
                    mu = max(mu / 3.0, 1e-12)
                    break
                mu *= 10.0
            if accepted is None:
                # 加大阻尼也找不到更好的点：在当前点收敛 / no better point even with heavy damping: converged here
                converged = point.feasible
                break
            gain = (point.phi - accepted.phi) / max(point.phi, 1e-300)
            point = accepted
            calm = calm + 1 if gain < 1e-10 and point.feasible else 0
            if on_progress is not None:
                on_progress({"merit": point.merit, "iteration": it, "violation": point.violation,
                             "elapsed_s": round(time.monotonic() - started, 1)})
            if calm >= 2:
                converged = True
                break
    except Stop:
        stopped_by = budget.stopped_by
    finally:
        problem.budget = None
        problem.apply(point.x)
    return {"initial": initial, "final": point, "iterations": it, "converged": converged, "stopped_by": stopped_by,
            "elapsed_s": round(time.monotonic() - started, 3)}


def _better(trial: Point, point: Point) -> bool:
    """接受准则：违背不增加且残差下降；或者当前违背时，最大违背或违背的二范数减小。
    Acceptance: less residual without more violation, or, while violated, a smaller worst or total violation."""
    tol = FEASIBLE_TOL
    if trial.violation <= max(point.violation, tol) * (1 + 1e-12) and trial.phi < point.phi * (1 - 1e-15):
        return True
    if point.violation <= tol:
        return False
    return trial.violation < point.violation * (1 - 1e-7) or (
        trial.violation <= point.violation * (1 + 1e-12) and trial.violation_norm < point.violation_norm * (1 - 1e-7))


def _failed_rows(problem: Problem) -> dict[str, str]:
    result = problem.evaluator.evaluate()
    return {str(k + 1): v for k, v in list(result.failed.items())[:20]}


def summary(run: dict[str, Any], problem: Problem) -> dict[str, Any]:
    """ZOS 后端 optimize 的结果字段，另加约束与收敛信息。/ The ZOS optimize result fields, plus constraints and convergence."""
    from saos.budget import outcome

    initial, final = run["initial"], run["final"]
    improved = outcome(initial.merit, final.merit, converged=run["converged"], stopped_by=run["stopped_by"])["improved"]
    violated = [problem_label for problem_label, v in zip(final.labels, (np.maximum(0.0, -final.g) / final.scale) if final.g.size else [])
                if v > FEASIBLE_TOL]
    return {
        "initial_merit": initial.merit, "final_merit": final.merit, "variables": len(problem.keys),
        "targets": int(initial.r.size), "constraints": int(initial.g.size),
        "succeeded": bool(improved and run["stopped_by"] is None and final.feasible),
        "solver_succeeded": final.feasible, "improved": improved, "converged": run["converged"],
        "stopped_by": run["stopped_by"], "elapsed_s": run["elapsed_s"], "iterations": run["iterations"],
        "evaluations": problem.evaluations, "algorithm": "dls",
        "constraint_handling": "exact (active set, Lagrange multipliers)",
        "violation": {"before": initial.violation, "after": final.violation, "violated_after": violated[:20]},
    }


# ---- Hammer 与全局搜索 / Hammer and global search ----

@dataclass
class Candidate:
    merit: float
    x: np.ndarray
    glasses: dict[int, str]


def _local_task(problem: Problem, x0: np.ndarray, glasses: dict[int, str], iterations: int, seconds: float,
                should_cancel: Callable[[], bool] | None = None) -> dict[str, Any] | None:
    """从 x0、换好的玻璃出发跑一次局部优化；起点算不出（BAD_ARGS）或换料不受支持时返回 None。主进程与 worker 共用。
    One local run from x0 with the given glasses; None when the start cannot be computed or the substitution is refused."""
    try:
        for s, name in glasses.items():
            problem.backend.set_param(f"{s}.material", name)
        run = local(problem, iterations=iterations, deadline=time.monotonic() + seconds, should_cancel=should_cancel, start=x0)
    except SaosError as exc:
        if exc.code not in (ErrorCode.BAD_ARGS, ErrorCode.BACKEND_UNSUPPORTED):
            raise
        return None
    final = run["final"]
    return {"x": final.x, "merit": final.merit, "feasible": final.feasible, "stopped_by": run["stopped_by"]}


_WORKER: Problem | None = None


def _worker_init(blob: bytes) -> None:
    """worker 进程：内核单线程（并行已在进程这一层），载入主进程 pickle 过来的整份 Problem（含只在内存里的变量与换料）。"""
    import pickle

    from numba import set_num_threads

    global _WORKER
    set_num_threads(1)
    _WORKER = pickle.loads(blob)


def _worker_task(x0: np.ndarray, glasses: dict[int, str], iterations: int, seconds: float) -> dict[str, Any] | None:
    # SaosError 的构造参数跨进程 pickle 不回来，换成文字由主进程再抛 / SaosError travels back as text
    try:
        return _local_task(_WORKER, x0, glasses, iterations, seconds)
    except SaosError as exc:
        return {"error": f"{exc.code}: {exc}"}


def search(problem: Problem, kind: str, *, max_seconds: float, substitutes: dict[int, list[str]],
           should_cancel: Callable[[], bool] | None = None, on_progress: Callable[[dict[str, Any]], None] | None = None,
           seed: int | None = None, local_iterations: int = 40, rel: float | None = None,
           checkpoint_s: float | None = None, workers: int = 1) -> dict[str, Any]:
    """Hammer（kind="hammer"）或全局搜索（kind="global"），到 max_seconds 或取消为止。substitutes 为 {面: 可换的牌号}。
    每隔 checkpoint_s（默认 budget.checkpoint_interval）记一个检查点（最好的 merit 与它出现的时刻 best_at_s）；给了 rel 时，
    两个检查点之间有新系统试完、而最好的 merit 相对下降不到 rel，就提前停（stopped_by="stalled"）。
    workers > 1 时，第一次局部优化之后的候选在 spawn 进程池里并行试（每个 worker 一份 pickle 的 Problem，内核单线程），
    每个候选从提交时的最好解扰动出发；结果到达的顺序不定，同一个 seed 不再能复现。
    返回 best（后端已是这个解）、kept（全局搜索最好的若干个）、checkpoints 与统计。
    Hammer or global search until max_seconds or a cancel; substitutes maps surfaces to replaceable glasses. A checkpoint
    every checkpoint_s records the best merit and when it was found; with rel, the run stops early when systems finished
    since the last checkpoint gained less than rel relative to it. With workers > 1 the candidates after the first local
    run are tried in a spawned process pool; results arrive in any order, so a seed no longer reproduces the run."""
    if kind not in ("hammer", "global"):
        raise SaosError(ErrorCode.BAD_ARGS, "kind must be hammer or global")
    seed = int(time.time_ns() % (2**32)) if seed is None else int(seed)
    rng = np.random.default_rng(seed)
    started = time.monotonic()
    deadline = started + float(max_seconds)
    interval = checkpoint_interval(max_seconds) if checkpoint_s is None else float(checkpoint_s)
    backend = problem.backend
    start_x = problem.values()
    start_glass = {s: str(backend.get_param(f"{s}.material")) for s in substitutes}
    first = local(problem, iterations=local_iterations, deadline=deadline, should_cancel=should_cancel, on_progress=on_progress)
    initial_merit = first["initial"].merit
    best = Candidate(first["final"].merit if first["final"].feasible else math.inf, first["final"].x.copy(), dict(start_glass))
    kept: list[Candidate] = [best] if math.isfinite(best.merit) else []
    systems = 1
    best_at_s = round(time.monotonic() - started, 3) if math.isfinite(best.merit) else None
    checkpoints = [{"elapsed_s": best_at_s or 0.0, "systems": 1, "merit": best.merit if math.isfinite(best.merit) else None,
                    "best_at_s": best_at_s}]
    stopped_by = first["stopped_by"]

    def propose() -> tuple[np.ndarray, dict[int, str]]:
        base = best if kind == "hammer" or not kept else Candidate(math.inf, start_x, dict(start_glass))
        sigma = 0.5 if kind == "hammer" else 2.0
        x0 = _keep_signs(problem, base.x, base.x + sigma * problem.spans * rng.standard_normal(len(problem.keys)))
        glasses = dict(base.glasses)
        if substitutes and (kind == "global" or rng.random() < 0.5):
            for s in (list(substitutes) if kind == "global" else [int(rng.choice(list(substitutes)))]):
                if substitutes[s]:
                    glasses[s] = str(substitutes[s][int(rng.integers(len(substitutes[s])))])
        return x0, glasses

    def take(run: dict[str, Any] | None, glasses: dict[int, str]) -> str | None:
        """记一个试完的系统：更新最好解、检查点与进度，返回停止原因。/ Record one finished system; returns why to stop."""
        nonlocal best, best_at_s, kept, systems
        if run is not None and "error" in run:
            raise SaosError(ErrorCode.INTERNAL_ERROR, f"a search worker failed: {run['error']}")
        systems += 1
        elapsed = round(time.monotonic() - started, 3)
        if run is not None and run["feasible"]:
            found = Candidate(run["merit"], np.asarray(run["x"]).copy(), dict(glasses))
            if found.merit < best.merit * (1 - 1e-12):
                best, best_at_s = found, elapsed
            if kind == "global":
                kept = sorted(kept + [found], key=lambda c: c.merit)[:SEARCH_KEEP]
        reason = run["stopped_by"] if run is not None else None
        merit = best.merit if math.isfinite(best.merit) else None
        last = checkpoints[-1]
        if elapsed - last["elapsed_s"] >= interval:
            checkpoints.append({"elapsed_s": elapsed, "systems": systems, "merit": merit, "best_at_s": best_at_s})
            if reason is None and stalled(last["merit"], merit, rel):
                reason = "stalled"
        if on_progress is not None:
            # 回调导出的完整镜头必须与所报告的最好评价函数值对应，包含换料结果。
            if math.isfinite(best.merit):
                for s, name in best.glasses.items():
                    backend.set_param(f"{s}.material", name)
                problem.apply(best.x)
            on_progress({"merit": merit, "systems": systems, "elapsed_s": round(elapsed, 1), "best_at_s": best_at_s})
        return reason

    def halted() -> str | None:
        if should_cancel is not None and should_cancel():
            return "cancel"
        return "timeout" if time.monotonic() > deadline else None

    pool = None
    try:
        if workers > 1 and stopped_by is None:
            import multiprocessing
            import pickle

            pool = multiprocessing.get_context("spawn").Pool(workers, _worker_init, (pickle.dumps(problem),))
            pending: list[tuple[Any, dict[int, str]]] = []
            while stopped_by is None:
                stopped_by = halted()
                while stopped_by is None and len(pending) < workers:
                    x0, glasses = propose()
                    job = pool.apply_async(_worker_task, (x0, glasses, local_iterations, deadline - time.monotonic()))
                    pending.append((job, glasses))
                done = [item for item in pending if item[0].ready()]
                if not done:
                    time.sleep(0.02)
                for item in done:
                    pending.remove(item)
                    stopped_by = take(item[0].get(), item[1]) or stopped_by
        while stopped_by is None:
            stopped_by = halted()
            if stopped_by is None:
                x0, glasses = propose()
                run = _local_task(problem, x0, glasses, local_iterations, deadline - time.monotonic(), should_cancel)
                stopped_by = take(run, glasses)
    finally:
        if pool is not None:
            # worker 只在内存里计算、不写文件：直接终止，取消后不再占核 / workers only compute in memory: terminate them
            pool.terminate()
            pool.join()
        # 后端停在最好解；一个可行解都没有时回到起点 / the backend ends on the best solution, or on the start without any
        final = best if math.isfinite(best.merit) else Candidate(math.inf, start_x, dict(start_glass))
        for s, name in final.glasses.items():
            backend.set_param(f"{s}.material", name)
        problem.apply(final.x)
    return {"best": final, "kept": kept, "systems": systems, "initial_merit": initial_merit, "stopped_by": stopped_by,
            "elapsed_s": round(time.monotonic() - started, 3), "seed": seed, "start_glass": start_glass,
            "best_at_s": best_at_s, "checkpoints": checkpoints, "workers": workers if pool is not None else 1}


def _keep_signs(problem: Problem, base: np.ndarray, x: np.ndarray) -> np.ndarray:
    """扰动后的正厚度不翻成负值或零：最多缩到原来的 20%。/ A positive thickness stays positive: at most down to 20%."""
    out = x.copy()
    for j, key in enumerate(problem.keys):
        if key.endswith((".thickness", ".semi_diameter")) and base[j] > 0:
            out[j] = max(out[j], 0.2 * base[j])
        lo_hi = problem.bounds[j]
        if lo_hi is not None:
            out[j] = min(max(out[j], lo_hi[0]), lo_hi[1])
    return out
