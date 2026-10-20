"""显微物镜配方的命令行：saos microscope …（复现、验收、扰动、适配、结构库）。"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from saos.protocol import read_json, write_json
from saos.recipes.microscope import calls, library_cli


def cmd_op(args: argparse.Namespace) -> Any:
    kwargs: dict[str, Any] = {}
    if args.action == "repro":
        kwargs["expect"] = read_json(args.expect)
    elif args.action == "accept":
        kwargs["spec"] = args.spec
    elif args.action == "perturb":
        kwargs.update(seed=args.seed, radius_rel=args.radius_rel, thickness_rel=args.thickness_rel)
    return calls.call(args.action, args.job, args.timeout, **kwargs)


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
    group = sub.add_parser("microscope", help="microscope objective recipe")
    msub = group.add_subparsers(dest="action", required=True, parser_class=parser_class)

    def op_cmd(name: str, help_text: str) -> argparse.ArgumentParser:
        p = msub.add_parser(name, help=help_text)
        p.add_argument("--job", required=True)
        p.add_argument("--timeout", type=float, default=default_timeout)
        p.set_defaults(func=cmd_op)
        return p

    p = op_cmd("repro", "reproduction check: focal length, NA, working distance and magnification against expected values")
    p.add_argument("expect", help="expected values JSON")
    p = op_cmd("accept", "acceptance evaluation of the saved lens against the optical spec")
    p.add_argument("--spec", required=True, help="spec file inside the job, e.g. sources/spec.yaml or spec/benchmark.yaml")
    p = op_cmd("perturb", "controlled perturbation of objective radii and thicknesses with a fixed seed")
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--radius-rel", type=float, required=True, help="relative curvature change limit, e.g. 0.01")
    p.add_argument("--thickness-rel", type=float, default=0.0)

    p = msub.add_parser("adapt", help="patent/textbook extraction → standard-layout prescription + expected values")
    p.add_argument("extract", help="extraction JSON")
    p.add_argument("--embodiment", help="embodiment key in a schema-1 extraction")
    p.add_argument("--focal-length", type=float, help="target objective focal length in mm (default: as printed for mm data, 200/|β| for normalized data)")
    p.add_argument("--tube-lens", type=float, default=200.0)
    p.add_argument("--field-number", type=float, help="default: field number or image height from the extraction")
    p.add_argument("--catalogs", nargs="+", default=["OHARA", "SCHOTT", "HOYA", "SUMITA", "NIKON-HIKARI", "CDGM"])
    p.add_argument("--stop", help="rear_focal, printed or r<surface>; default: the printed stop if any, else rear_focal")
    p.add_argument("--out", required=True, help="output directory")
    p.set_defaults(func=cmd_adapt)

    library_cli.register(msub, parser_class, default_timeout)
    from saos.cli_help import configure
    from saos.recipes.microscope import cli_help

    configure(group, "microscope", commands=cli_help.COMMANDS, groups=cli_help.GROUPS)
