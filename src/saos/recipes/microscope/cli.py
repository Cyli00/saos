"""显微物镜配方的命令行：saos microscope …（原 zea 的 lens repro/perturb、eval、round、adapt、library、interview、plan）。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from saos.protocol import read_json, write_json
from saos.recipes.microscope import calls, library_cli, plan_cli


def cmd_op(args: argparse.Namespace) -> Any:
    kwargs: dict[str, Any] = {}
    if args.action == "repro":
        kwargs["expect"] = read_json(args.expect)
    elif args.action == "accept":
        kwargs["spec"] = args.spec
    elif args.action == "perturb":
        kwargs.update(seed=args.seed, radius_rel=args.radius_rel, thickness_rel=args.thickness_rel)
    elif args.action == "round":
        kwargs.update(spec=args.spec, note=args.note)
    fn = "record_round" if args.action == "round" else args.action
    return calls.call(fn, args.job, args.timeout, **kwargs)


def cmd_adapt(args: argparse.Namespace) -> Any:
    from saos.recipes.microscope import adapt

    out = adapt.from_extraction(
        read_json(args.extract),
        embodiment=args.embodiment,
        focal_length_mm=args.focal_length,
        tube_lens_mm=args.tube_lens,
        field_number=args.field_number,
        catalogs=args.catalogs,
        stop=args.stop,
    )
    out_dir = Path(args.out)
    written = {}
    for key, name in (("prescription", "prescription.json"), ("expect", "expect.json"), ("report", "adapt_report.json")):
        write_json(out_dir / name, out[key])
        written[key] = str(out_dir / name)
    return {"written": written, "expect": out["expect"], "report": out["report"]}


def register(sub, parser_class, default_timeout: float) -> None:
    group = sub.add_parser("microscope", help="显微物镜配方")
    msub = group.add_subparsers(dest="action", required=True, parser_class=parser_class)

    def op_cmd(name: str, help_text: str) -> argparse.ArgumentParser:
        p = msub.add_parser(name, help=help_text)
        p.add_argument("--job", required=True)
        p.add_argument("--timeout", type=float, default=default_timeout)
        p.set_defaults(func=cmd_op)
        return p

    p = op_cmd("repro", "复现校验：焦距、数值孔径、工作距离、放大率与期望值比较")
    p.add_argument("expect", help="期望值 JSON")
    p = op_cmd("accept", "按光学规格对当前已保存的镜头做验收评估")
    p.add_argument("--spec", help="任务目录内的规格文件，默认用 job.json 中的 spec")
    p = op_cmd("perturb", "受控扰动：按固定随机种子改变物镜各面曲率和厚度")
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--radius-rel", type=float, required=True, help="曲率相对变化上限，如 0.01")
    p.add_argument("--thickness-rel", type=float, default=0.0)
    p = op_cmd("round", "把当前镜头存为下一轮，做验收评估并给出是否停机")
    p.add_argument("--spec", help="第 0 轮必须给出；之后沿用，不能更换")
    p.add_argument("--note", default="", help="本轮调整说明")

    p = msub.add_parser("adapt", help="专利/教材面数据提取结果 → 标准设计布局的处方 + 复现期望值")
    p.add_argument("extract", help="提取结果 JSON")
    p.add_argument("--embodiment", help="schema 1 提取结果中的实施例键名")
    p.add_argument("--focal-length", type=float, help="缩放到的物镜焦距 mm；默认毫米制按原值，归一化数据按 200/|β|")
    p.add_argument("--tube-lens", type=float, default=200.0)
    p.add_argument("--field-number", type=float, help="默认取提取结果中的视场数或像高")
    p.add_argument("--catalogs", nargs="+", default=["OHARA", "SCHOTT", "HOYA", "SUMITA", "NIKON-HIKARI", "CDGM"])
    p.add_argument("--stop", help="rear_focal、printed 或 r<面号>；默认有印出的光阑就用，否则 rear_focal")
    p.add_argument("--out", required=True, help="输出目录")
    p.set_defaults(func=cmd_adapt)

    library_cli.register(msub, parser_class, default_timeout)
    plan_cli.register(msub, parser_class)
