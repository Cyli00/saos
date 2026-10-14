"""优化基准命令：bench split / plan / calibrate / run（入口见 run.py）。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from saos.recipes.microscope import library
import calibration
import runner
from saos.protocol import ErrorCode, SaosError, write_json


def cmd_split(args: argparse.Namespace) -> Any:
    result = library.split_benchmarks(library.all_entries())
    path = library.library_dir() / "benchmarks.json"
    write_json(path, result)
    return {"written": str(path), **result}


def cmd_bench_plan(args: argparse.Namespace) -> Any:
    data = calibration.write_plan(args.batch, args.policy)
    counts: dict[str, int] = {}
    for t in data["tasks"]:
        key = f"{t['group']}/{t['class']}"
        counts[key] = counts.get(key, 0) + 1
    return {"manifest": str(calibration.manifest_path(args.batch)), "code_sha256": data["code_sha256"], "tasks": len(data["tasks"]), "by_group_class": counts}


def cmd_bench_calibrate(args: argparse.Namespace) -> Any:
    manifest = calibration.load_plan(args.batch)
    got = calibration.collect(manifest, [Path(h) for h in args.homes])
    pick = lambda group, cls: [s for s in got["samples"] if s["group"] == group and s["class"] == cls]  # noqa: E731
    ready = lambda group, cls: calibration.readiness(manifest, got, group, cls)  # noqa: E731
    out: dict[str, Any] = {
        "batch": args.batch,
        "excluded": got["excluded"],
        "duplicates": got["duplicates"],
        "conflicts": got["conflicts"],
        "tuning_main": calibration.select(pick("tuning", "main"), ready("tuning", "main")),
    }
    if args.validate:
        n, rel, cap = int(args.validate[0]), float(args.validate[1]), int(args.validate[2])
        out["validation_main"] = calibration.validate(pick("validation", "main"), n, rel, cap, ready("validation", "main"))
        out["stress"] = {g: calibration.validate(pick(g, "stress"), n, rel, cap, ready(g, "stress")) for g in ("tuning", "validation")}
    # 放在最前面：只看命令结果时也能看到参数是否已校准
    return {"calibration_status": calibration.overall_status(out["tuning_main"], out.get("validation_main")), **out}


def cmd_bench_run(args: argparse.Namespace) -> Any:
    if args.batch:
        if not args.task:
            raise SaosError(ErrorCode.BAD_ARGS, "--batch 需要同时给出 --task")
        return runner.run_task(args.batch, args.task, args.timeout, args.cores, args.discard)
    if not args.entry:
        raise SaosError(ErrorCode.BAD_ARGS, "需要条目编号，或用 --batch/--task 运行正式任务")
    stretch = {k: float(v) for k, v in (x.split("=", 1) for x in args.stretch)}
    return runner.run_case(
        args.entry, args.seed, args.radius_rel, args.thickness_rel, args.max_rounds, args.timeout, args.job, not args.no_amplify, stretch, args.cores, args.discard
    )


def register(sub, parser_class, default_timeout: float) -> None:
    bench_parser = sub.add_parser("bench", help="优化基准")
    bsub = bench_parser.add_subparsers(dest="action", required=True, parser_class=parser_class)
    bsub.add_parser("split", help="把可作基准的条目分为调参组和验证组").set_defaults(func=cmd_split)
    p = bsub.add_parser("plan", help="生成正式批次的实验清单（library/calibration/<批次>.json），记录当前代码版本")
    p.add_argument("--batch", required=True)
    p.add_argument("--policy", default=runner.POLICY)
    p.set_defaults(func=cmd_bench_plan)
    p = bsub.add_parser("calibrate", help="按实验清单准入正式轨迹，在调参组上选停机参数；给出 --validate 时在验证组上检验")
    p.add_argument("--batch", required=True)
    p.add_argument("--homes", nargs="+", required=True, help="运行过该批次的各 SAOS_HOME")
    p.add_argument("--validate", nargs=3, metavar=("N", "REL", "CAP"), help="固定的停机参数，例如 3 0.01 20")
    p.set_defaults(func=cmd_bench_calibrate)
    p = bsub.add_parser("run", help="受控扰动后按参考策略逐轮优化，记录完整轨迹（需要后台进程）")
    p.add_argument("entry", nargs="?")
    p.add_argument("--batch", help="正式批次：按实验清单运行 --task 指定的任务")
    p.add_argument("--task")
    p.add_argument("--seed", type=int, default=library.PERTURBATION["seed"])
    p.add_argument("--radius-rel", type=float, default=library.PERTURBATION["radius_rel"])
    p.add_argument("--thickness-rel", type=float, default=library.PERTURBATION["thickness_rel"])
    p.add_argument("--max-rounds", type=int, default=20)
    p.add_argument("--no-amplify", action="store_true", help="只用给定幅度扰动，不逐级放大")
    p.add_argument("--stretch", nargs="+", default=[], metavar="KEY=F", help="拉伸规格：wd=1.25 表示工作距离下限取原结构实测值的 1.25 倍；length=0.85 同理")
    p.add_argument("--cores", type=int, help="每次局部优化使用的线程数；并行运行多个后台进程时用来分配 CPU")
    p.add_argument("--discard", action="store_true", help="后台进程当前加载的镜头有未保存修改时放弃它；默认拒绝")
    p.add_argument("--job", help="继续一个已有的基准任务")
    p.add_argument("--timeout", type=float, default=default_timeout)
    p.set_defaults(func=cmd_bench_run)
