"""分项耗时：关键入口累计调用次数和耗时，daemon 把每次操作期间的增量写进执行记录的 phases。

各分项是包含式计时：optimize 的耗时包含其中的 trace 和 merit，所以分项之间不互斥，不能直接相加。
Phases are inclusive: an optimize includes its traces and merits, so the phases overlap and must not be summed.
"""

from __future__ import annotations

import functools
import threading
import time
from typing import Any, Callable

_lock = threading.Lock()
_totals: dict[str, list[float]] = {}  # 名称 -> [调用次数, 秒] / name -> [calls, seconds]


def add(name: str, seconds: float) -> None:
    with _lock:
        entry = _totals.setdefault(name, [0, 0.0])
        entry[0] += 1
        entry[1] += seconds


def timed(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def inner(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                add(name, time.perf_counter() - started)

        return inner

    return wrap


def snapshot() -> dict[str, tuple[int, float]]:
    with _lock:
        return {name: (int(calls), seconds) for name, (calls, seconds) in _totals.items()}


def delta(before: dict[str, tuple[int, float]], after: dict[str, tuple[int, float]]) -> dict[str, dict[str, float]]:
    """两次快照之间的增量，只保留有变化的分项。注意多个线程同时计时时，增量会包含别的操作的调用。"""
    out: dict[str, dict[str, float]] = {}
    for name, (calls, seconds) in after.items():
        base_calls, base_seconds = before.get(name, (0, 0.0))
        if calls != base_calls:
            out[name] = {"calls": calls - base_calls, "s": round(seconds - base_seconds, 4)}
    return out
