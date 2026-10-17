"""搜索的控制逻辑：网格、一维求根、无导数寻优。每次评估仍由调用方经 ZOS-API 完成（ADR-0008），这里只决定下一个点。
Search control logic: grids, 1-D root finding, derivative-free minimisation. Every evaluation is still the caller's
ZOS-API call (ADR-0008); this module only decides where to evaluate next.

评估函数返回 None、inf 或 NaN 表示这个点无效（零件交叠、没有实焦点……）：求根时跳过，寻优时当作很差的点。
An evaluation returning None, inf or NaN marks an invalid point (overlapping parts, no real focus, …): root finding
skips it, minimisation treats it as a very bad point.
"""

from __future__ import annotations

import itertools
import math
import time
from typing import Any, Callable, Iterable, Sequence

import numpy as np

# 无效点在寻优中的取值：比任何有效值都差，但仍是有限数，单纯形不会因 inf 失去方向
# the value of an invalid point during minimisation: worse than any valid value but finite, so the simplex keeps its bearings
INVALID = 1e30


class Stop(Exception):
    """预算用完或收到取消请求。/ Budget used up or cancel requested."""


def _valid(v: Any) -> bool:
    return v is not None and isinstance(v, (int, float, np.floating)) and math.isfinite(float(v))


def axis(spec: Any) -> list[float]:
    """一个轴的取值：列表原样；{"from", "to", "steps"} 等分（含两端）；{"from", "to", "step"} 按步长。
    Values of one axis: a list as is; {"from", "to", "steps"} evenly spaced, ends included; {"from", "to", "step"} by step."""
    if isinstance(spec, dict):
        lo, hi = float(spec["from"]), float(spec["to"])
        if "steps" in spec:
            return [float(v) for v in np.linspace(lo, hi, int(spec["steps"]))]
        step = abs(float(spec["step"])) * (1 if hi >= lo else -1)
        count = int(math.floor((hi - lo) / step + 1e-9)) + 1
        return [lo + k * step for k in range(count)]
    if isinstance(spec, (list, tuple)):
        return [float(v) if isinstance(v, (int, float)) else v for v in spec]
    return [spec]


def grid(axes: dict[str, Any]) -> list[dict[str, Any]]:
    """各轴取值的笛卡尔积，每个点是 {键: 值}。/ The Cartesian product of the axes, each point a {key: value} dict."""
    keys = list(axes)
    return [dict(zip(keys, combo)) for combo in itertools.product(*(axis(axes[k]) for k in keys))]


class Budget:
    """评估次数、时长和取消的统一检查。/ One place to check evaluation count, wall time and cancellation."""

    def __init__(self, max_evals: int | None = None, max_seconds: float | None = None, should_stop: Callable[[], bool] | None = None):
        self.max_evals, self.max_seconds, self.should_stop = max_evals, max_seconds, should_stop
        self.started = time.monotonic()
        self.evals = 0

    def tick(self) -> None:
        if self.max_evals is not None and self.evals >= self.max_evals:
            raise Stop("max_evals")
        if self.max_seconds is not None and time.monotonic() - self.started > self.max_seconds:
            raise Stop("max_seconds")
        if self.should_stop is not None and self.should_stop():
            raise Stop("cancelled")
        self.evals += 1


def root(f: Callable[[float], Any], lo: float, hi: float, *, target: float = 0.0, scan: int = 9, xtol: float | None = None,
         max_evals: int = 60, should_stop: Callable[[], bool] | None = None) -> dict[str, Any]:
    """在 [lo, hi] 内找 f(x) = target。先等分扫 scan 个点找变号（跳过无效点），再用 Brent 法收敛到 xtol；
    比"粗扫 17 点 + 二分 14 次"少一半以上的评估。返回 {x, f, ok, evals, reason, bracket, history}；
    没有变号时 ok 为 False，x 取 |f − target| 最小的有效点。最后一次评估不一定在 x 上：用前先把变量设回 x。
    Find f(x) = target in [lo, hi]. Scan `scan` evenly spaced points for a sign change (invalid points skipped), then
    converge with Brent's method to xtol; well under half the evaluations of a 17-point scan plus 14 bisections.
    Returns {x, f, ok, evals, reason, bracket, history}; without a sign change ok is False and x is the valid point with
    the smallest |f − target|. The last evaluation need not be at x: set the variable back to x before using it."""
    from scipy.optimize import brentq

    xtol = xtol if xtol is not None else abs(hi - lo) * 1e-6
    budget = Budget(max_evals, None, should_stop)
    history: list[list[Any]] = []

    def g(x: float) -> float | None:
        budget.tick()
        v = f(float(x))
        v = float(v) - target if _valid(v) else None
        history.append([float(x), None if v is None else v + target])
        return v

    points = [(x, g(x)) for x in np.linspace(lo, hi, max(2, scan))]
    valid = [(x, v) for x, v in points if v is not None]
    bracket = next(((a, b) for a, b in zip(valid, valid[1:]) if a[1] == 0 or a[1] * b[1] < 0), None)
    if bracket is None and valid and valid[-1][1] == 0:
        bracket = (valid[-1], valid[-1])
    if bracket is None:
        best = min(valid, key=lambda p: abs(p[1]), default=None)
        return {"x": None if best is None else best[0], "f": None if best is None else best[1] + target, "ok": False, "evals": budget.evals,
                "reason": "no valid point" if best is None else "no sign change in [lo, hi]", "bracket": None, "history": history}
    (a, fa), (b, fb) = bracket
    if fa == 0 or a == b:
        return {"x": a, "f": target, "ok": True, "evals": budget.evals, "reason": "exact", "bracket": [a, b], "history": history}

    def h(x: float) -> float:
        v = g(x)
        if v is None:
            raise ValueError(f"invalid point at {x} inside the bracket [{a}, {b}]")
        return v

    try:
        x = float(brentq(h, a, b, xtol=xtol, maxiter=max_evals))
        at = min(history, key=lambda p: abs(p[0] - x))
        return {"x": x, "f": at[1], "ok": True, "evals": budget.evals,
                "reason": "converged", "bracket": [a, b], "history": history}
    except (ValueError, RuntimeError, Stop) as exc:
        last = [p for p in history if p[1] is not None and a <= p[0] <= b] or [[a, fa + target]]
        best = min(last, key=lambda p: abs(p[1] - target))
        return {"x": best[0], "f": best[1], "ok": False, "evals": budget.evals, "reason": f"{type(exc).__name__}: {exc}",
                "bracket": [a, b], "history": history}


def minimize(f: Callable[[dict[str, float]], Any], start: dict[str, float], step: dict[str, float], *,
             bounds: dict[str, Sequence[float | None]] | None = None, method: str = "nelder-mead", max_evals: int = 200,
             max_seconds: float | None = None, xtol: float = 1e-3, ftol: float = 1e-6,
             should_stop: Callable[[], bool] | None = None, on_eval: Callable[[dict[str, Any]], Any] | None = None) -> dict[str, Any]:
    """无导数寻优（scipy 的 Nelder–Mead 或 Powell）。变量按 step 归一化，xtol 是相对 step 的收敛阈值；无效点记为 INVALID。
    f 收到 {键: 值}，返回目标值（越小越好）或 None/inf。返回 {best, best_f, start_f, evals, stopped, history}；
    预算用完或取消时保留已找到的最好点。
    Derivative-free minimisation (scipy's Nelder–Mead or Powell). Variables are scaled by step; xtol is relative to step;
    invalid points count as INVALID. f receives {key: value} and returns the objective (lower is better) or None/inf.
    Returns {best, best_f, start_f, evals, stopped, history}; the best point so far is kept when the budget runs out."""
    from scipy.optimize import minimize as sp_minimize

    keys = list(start)
    x0 = np.array([float(start[k]) for k in keys])
    scale = np.array([abs(float(step[k])) or 1.0 for k in keys])
    budget = Budget(max_evals, max_seconds, should_stop)
    history: list[dict[str, Any]] = []
    best: dict[str, Any] = {"f": math.inf, "x": None}
    sp_bounds = None
    if bounds:
        sp_bounds = []
        for k, x, s in zip(keys, x0, scale):
            lo, hi = (bounds.get(k) or (None, None))
            sp_bounds.append((None if lo is None else (float(lo) - x) / s, None if hi is None else (float(hi) - x) / s))

    def point(u: np.ndarray) -> dict[str, float]:
        return {k: float(v) for k, v in zip(keys, x0 + u * scale)}

    def objective(u: np.ndarray) -> float:
        budget.tick()
        p = point(u)
        v = f(p)
        value = float(v) if _valid(v) else None
        record = {"x": p, "f": value}
        history.append(record)
        if on_eval is not None:
            on_eval(record)
        if value is not None and value < best["f"]:
            best.update(f=value, x=p)
        return INVALID if value is None else value

    stopped = "converged"
    try:
        if method == "nelder-mead":
            simplex = np.vstack([np.zeros(len(keys)), np.eye(len(keys))])
            options = {"initial_simplex": simplex, "xatol": xtol, "fatol": ftol, "maxfev": 10 ** 9, "adaptive": len(keys) > 4}
            sp_minimize(objective, np.zeros(len(keys)), method="Nelder-Mead", bounds=sp_bounds, options=options)
        elif method == "powell":
            sp_minimize(objective, np.zeros(len(keys)), method="Powell", bounds=sp_bounds,
                        options={"xtol": xtol, "ftol": ftol, "maxfev": 10 ** 9, "direc": np.eye(len(keys))})
        else:
            raise ValueError(f"unknown method {method!r}: nelder-mead or powell")
    except Stop as exc:
        stopped = str(exc)
    return {"best": best["x"], "best_f": None if best["x"] is None else best["f"], "start": dict(start),
            "start_f": history[0]["f"] if history else None, "evals": budget.evals, "stopped": stopped,
            "seconds": round(time.monotonic() - budget.started, 2), "history": history}


def ranked(rows: Iterable[dict[str, Any]], key: str = "shortfall") -> list[dict[str, Any]]:
    """按 key 从小到大排序，无效值排最后。/ Sort by key ascending, invalid values last."""
    return sorted(rows, key=lambda r: (not _valid(r.get(key)), float(r[key]) if _valid(r.get(key)) else 0.0))
