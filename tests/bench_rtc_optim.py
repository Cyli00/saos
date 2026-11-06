"""rtc DLS 一步的耗时拆分：求值（改参数与重算求解/自动半口径、评价函数）、其中内核追迹的份额、雅可比、一次迭代。

给加速方案（docs/plans/rtc-design-stage.md）定基线，不是测试，不进 pytest：
    uv run python tests/bench_rtc_optim.py --case iris_no_apertures --asphere 6 --rings 4 --arms 8
    RTC_THREADS=1 uv run python tests/bench_rtc_optim.py ...   # 单线程内核，对比进程池的单核效率
    uv run python tests/bench_rtc_optim.py --hammer 20 --workers 1 4 8   # Hammer 进程池的系统吞吐
只用 tests/fixtures/cases/ 的案例；--asphere N 把前 N 个光学面换成偶次非球面并放开 par2–par5，把变量数撑到实际设计的量级。
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from case_variants import BY_NAME
from rtc import kernel
from saos import prescription
from saos.backend import rtc_optim
from saos.backend.rtc import RtcBackend


class Timer:
    """包住一个函数，累计调用次数与秒数。/ Wraps a function and accumulates calls and seconds."""

    def __init__(self, owner, name: str):
        self.owner, self.name, self.original = owner, name, getattr(owner, name)
        self.calls, self.seconds = 0, 0.0

        def timed(*args, **kwargs):
            started = time.perf_counter()
            try:
                return self.original(*args, **kwargs)
            finally:
                self.calls += 1
                self.seconds += time.perf_counter() - started

        setattr(owner, name, timed)

    def reset(self) -> None:
        self.calls, self.seconds = 0, 0.0

    def restore(self) -> None:
        setattr(self.owner, self.name, self.original)


def build(case: str, asphere: int, rings: int, arms: int) -> RtcBackend:
    backend = RtcBackend(None, standalone=True)
    backend.build(prescription.normalize(BY_NAME[case].prescription()))
    image = len(backend.lens.surfaces) - 1
    optical = [s.index for s in backend.lens.surfaces[1:image] if s.radius not in (0.0, float("inf"))]
    for k, index in enumerate(optical):
        backend.vary(index, "radius", True)
        if k < asphere:
            backend.change_surface_type(index, "EvenAspheric")
            for n in range(2, 6):
                backend.vary(index, f"par{n}", True)
    for s in backend.lens.surfaces[1:image - 1]:
        if f"{s.index}.thickness" not in backend.held:
            backend.vary(s.index, "thickness", True)
    backend.merit_wizard({"rings": rings, "arms": arms, "glass": [1.0, 10.0, 1.0], "air": [0.5, 50.0, 0.5]})
    return backend


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # iris 的面孔径有意挡光，向导光线追不过去，用去掉孔径的变体 / iris clips the wizard rays by design
    parser.add_argument("--case", default="iris_no_apertures", choices=("sequential", "iris_no_apertures", "reflective"))
    parser.add_argument("--asphere", type=int, default=0, help="前 N 个光学面换成偶次非球面并放开 par2–par5")
    parser.add_argument("--rings", type=int, default=3)
    parser.add_argument("--arms", type=int, default=6)
    parser.add_argument("--repeat", type=int, default=5, help="求值与雅可比各重复几次取平均")
    parser.add_argument("--hammer", type=float, help="另跑这么多秒的 Hammer，比较 --workers 各档的系统吞吐")
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 4, 8], help="Hammer 的进程数，每档各跑一次")
    parser.add_argument("--output", type=Path, help="把结果写成 JSON")
    args = parser.parse_args()

    backend = build(args.case, args.asphere, args.rings, args.arms)
    problem = backend._problem()
    x = problem.values()
    base = problem.measure(x)  # 预热：numba 编译与缓存
    problem.jacobian(base)

    timers = {"apply": Timer(backend, "apply_values"), "recompute": Timer(backend, "_recompute"),
              "evaluate": Timer(problem.evaluator, "evaluate"), "kernel_trace": Timer(kernel, "trace")}
    try:
        started = time.perf_counter()
        for _ in range(args.repeat):
            problem.measure(x)
        measure_s = (time.perf_counter() - started) / args.repeat
        per_measure = {k: t.seconds / args.repeat for k, t in timers.items()}
        for t in timers.values():
            t.reset()
        started = time.perf_counter()
        for _ in range(args.repeat):
            problem.jacobian(base)
        jacobian_s = (time.perf_counter() - started) / args.repeat
        per_jacobian = {k: t.seconds / args.repeat for k, t in timers.items()}
        started = time.perf_counter()
        run = rtc_optim.local(problem, iterations=1)
        iteration_s = time.perf_counter() - started
    finally:
        for t in timers.values():
            t.restore()

    ms = lambda v: round(v * 1e3, 2)  # noqa: E731
    result = {
        "case": args.case, "asphere": args.asphere, "rings": args.rings, "arms": args.arms,
        "rtc_threads": os.environ.get("RTC_THREADS", "default (logical cores - 2)"),
        "variables": len(problem.keys), "residuals": int(base.r.size), "constraints": int(base.g.size),
        "measure_ms": ms(measure_s), "measure_split_ms": {k: ms(v) for k, v in per_measure.items()},
        "jacobian_ms": ms(jacobian_s), "jacobian_split_ms": {k: ms(v) for k, v in per_jacobian.items()},
        "jacobian_over_measure": round(jacobian_s / measure_s, 2),
        "one_iteration_ms": ms(iteration_s), "iteration_evaluations": problem.evaluations,
        "accepted": run["iterations"] >= 1 and run["final"].phi < run["initial"].phi,
    }
    if args.hammer:
        result["hammer"] = []
        for workers in args.workers:
            problem = build(args.case, args.asphere, args.rings, args.arms)._problem()
            out = rtc_optim.search(problem, "hammer", max_seconds=args.hammer, substitutes={}, seed=1, workers=workers)
            result["hammer"].append({"workers": workers, "systems": out["systems"], "elapsed_s": out["elapsed_s"],
                                     "systems_per_s": round(out["systems"] / out["elapsed_s"], 2),
                                     "initial_merit": out["initial_merit"], "best_merit": out["best"].merit})
    text = json.dumps(result, ensure_ascii=False, indent=1)
    print(text)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
