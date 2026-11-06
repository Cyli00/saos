"""CODE V 的 Glass Expert、Asphere Expert 与 saos 同名专家在 rtc 上的速度基准。

显式运行才启动 CODE V，不启动 OpticStudio，两边不要同时跑（会抢 CPU）：
    uv run python tests/bench_experts.py codev --lens <镜头.zmx> --output <新目录> --budget 600
    uv run python tests/bench_experts.py rtc --output <同一目录> --budget 600

rtc 必须在 codev 之后跑：它沿用 CODE V 的目标 EFL 和筛出的玻璃清单（USER_GLASS.dat）。
两边评价函数不同，只比用时和试探次数；降幅不能直接比。结果写在目录下的 codev.json 和 rtc.json。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from bench_codev import CodeV
from rtc import zmx

CODEV_MACROS = Path("C:/CodeV/macro")
GLASS_INPUTS = """Surfaces: {surfaces}
Reset Vignetting: NO
Catalogs Used: SCHOTT
User Catalog:
SCHOTT Availability:
OHARA Availability:
HOYA Availability:
HIKARI Availability:
NHG Availability:
CDGM Availability:
Delete Special Glasses:
Eliminate Discontinued:
Include Possible Inaccurate Dispersion:
Max Strip Price Factor:
Exclude Glasses Without Prices:
Max Index:
Min Index:
Max Stain Resistance:
Keep Unknown Stain:
Continue No Stain: YES
User Catalog After Filtering:
Absorption Factor: 0
CTE Mismatch: 0
Max Element Cost: 0
Max Total Cost: 0
Relative Weight: 0
"""
# 默认的直径下限 12 mm 会把小镜头全部排除 / the default 12 mm minimum diameter would exclude every surface of a small lens
ASPHERE_LIMITS = "ntm 2; sur sa; slo no; cvx no no; ccv no no; dia 0.001 100000; mxt 100000; aof n; go"


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def bounded(cv: CodeV, command: str, budget: float) -> tuple[float, bool, str]:
    start = time.monotonic()
    cv.call("AsyncCommand", command)
    stopped = False
    while cv.get("IsExecutingCommand"):
        cv.call("Wait", cv.int32(200))
        if not stopped and time.monotonic() - start > budget:
            cv.call("StopCommand")
            stopped = True
        if time.monotonic() - start > budget + 30:
            raise TimeoutError(f"CODE V 取消后 30 秒内未退出：{command}")
    return time.monotonic() - start, stopped, str(cv.get("CommandOutput"))


def run_codev(lens_file: Path, out: Path, budget: float) -> None:
    out.mkdir(parents=True)
    start_file = out / "start.zmx"
    shutil.copy(lens_file, start_file)
    lens = zmx.parse(start_file)
    last = len(lens.surfaces) - 2
    glass = [i for i, s in enumerate(lens.surfaces) if s.material is not None and 0 < i <= last]
    gaps = [i for i in range(1, last + 1) if i not in glass]
    result = {"lens": lens_file.name, "surfaces": last, "glass_surfaces": glass, "air_gaps": gaps}

    def evaluate(cv: CodeV, expression: str) -> str:
        return str(cv.call("EvaluateExpression", expression))

    with CodeV(out, budget + 60.0) as cv:
        result["version"] = evaluate(cv, "(cvv)")
        cv.command(f'in "{(CODEV_MACROS / "zemaxos_to_cv.seq").as_posix()}" "{start_file.as_posix()}"')
        cv.command("frz sa")
        cv.command("ca ape")
        result["efl"] = float(evaluate(cv, "(efl)"))
        (out / "autb.seq").write_text(f"der def\naut\nefl={result['efl']!r}\nmxc 50\ngo\n", encoding="ascii")
        for i in range(1, last + 1):
            cv.command(f"ccy s{i} 0")
        for i in gaps:
            cv.command(f"thc s{i} 0")
        for name in ("aut_first", "aut_second"):
            seconds, stopped, _ = bounded(cv, "in autb.seq", budget)
            result[name] = {"seconds": seconds, "stopped": stopped}
        cv.command('sav "start_opt.len"')

        ranks = []
        for _ in range(3):
            cv.command('res "start_opt.len"')
            ranks.append(bounded(cv, f'in cv_macro:AsphereExpert autb.seq "" "nas 0; {ASPHERE_LIMITS}"', budget)[0])
        result["asphere_rank_seconds"] = ranks

        cv.command('res "start_opt.len"')
        seconds, stopped, text = bounded(cv, f'in cv_macro:AsphereExpert autb.seq "" "nas 2; {ASPHERE_LIMITS}"', budget)
        (out / "asphere_place.txt").write_text(text, encoding="utf-8")
        result["asphere_place"] = {"seconds": seconds, "stopped": stopped}
        write_json(out / "codev.json", result)

        cv.command('res "start_opt.len"')
        (out / "inputs.dat").write_text(GLASS_INPUTS.format(surfaces=" ".join(map(str, glass))), encoding="ascii")
        seconds, stopped, text = bounded(cv, 'in cv_macro:GlassExpert autb.seq "" inputs.dat 7 N', budget)
        (out / "glass.txt").write_text(text, encoding="utf-8")
        best = re.findall(r"(?:Best error function obtained so far:|Error function:)\s*([\d.E+-]+)", text)
        initial = re.findall(r"Initial error function:\s*([\d.E+-]+)", text)
        result["glass"] = {"seconds": seconds, "stopped": stopped, "trials": len(re.findall(r"Trying next glass|accepted on S", text)),
                           "accepted": len(re.findall(r"accepted on S", text)), "initial": initial[0] if initial else None, "best": best[-1] if best else None}
    write_json(out / "codev.json", result)


def run_rtc(out: Path, budget: float) -> None:
    os.environ["SAOS_HOME"] = tempfile.mkdtemp(prefix="saos-bench-experts-")
    os.environ["SAOS_UI"] = "0"
    from saos import jobs
    from saos.backend.rtc import RtcBackend
    from saos.ctx.experts import AsphereExpert, GlassExpert
    from saos.daemon import Daemon
    from saos.scripting import ScriptContext

    codev = json.loads((out / "codev.json").read_text(encoding="utf-8"))

    def plain(name: str) -> str:
        return re.sub(r"[^A-Z0-9]", "", name.upper())

    catalog = (out / "USER_GLASS.dat").read_text(encoding="utf-8", errors="replace").splitlines()[1:]
    allowed = {plain(line.split("\t")[1]) for line in catalog if "\t" in line}

    def session(path: Path):
        backend = RtcBackend(None, standalone=True)
        daemon = Daemon(backend, backend.start())
        job = jobs.create_job(f"bench{time.monotonic_ns()}")
        for op, args in (("lens.import", {"src": str(path), "name": "a.zmx"}), ("lens.load", {"file": "a.zmx"})):
            reply = daemon.handle({"op": op, "args": {"job": job.id, **args}})
            if not reply["ok"]:
                raise RuntimeError(reply["error"])
        return daemon, ScriptContext(daemon, job, {})

    daemon, _ = session(out / "start.zmx")
    backend = daemon.backend
    for i in range(1, codev["surfaces"] + 1):
        backend.vary(i, "radius", True)
    # rtc 不能把求解改成变量，这样的像距留给求解 / rtc cannot turn a solve into a variable, so such an image distance stays solved
    for i in codev["air_gaps"]:
        if f"{i}.thickness" not in backend.held:
            backend.vary(i, "thickness", True)
    backend.merit_clear()
    backend.merit_add([{"id": "efl", "type": "EFFL", "params": [0, 1], "target": codev["efl"], "weight": 10.0}])
    backend.merit_wizard({"rings": 3, "arms": 6, "glass": [0.5, 30.0, 0.5], "air": [0.1, 100.0, 0.5], "start_at": 2})
    result = {"lens": codev["lens"], "allowed_glasses": len(allowed)}
    began = time.monotonic()
    optimised = backend.optimize("auto", max_seconds=budget)
    result["local"] = {"seconds": time.monotonic() - began, "merit": backend.merit_value(), "stopped_by": optimised.get("stopped_by")}
    start = out / "rtc_start_opt.zmx"
    backend.save_as(start)

    surfaces = list(range(1, codev["surfaces"] + 1))
    for engine in ("rtc", "opticstudio"):
        seconds, order = [], None
        for _ in range(3):
            _, ctx = session(start)
            began = time.monotonic()
            order = AsphereExpert(ctx, surfaces, terms=2, engine=engine, max_seconds=budget).rank().get("order")
            seconds.append(time.monotonic() - began)
        result[f"asphere_rank_{engine}"] = {"seconds": seconds, "order": order}

    daemon, ctx = session(start)
    began = time.monotonic()
    placed = AsphereExpert(ctx, surfaces, terms=2, engine="rtc", max_seconds=budget).run(nas=2)
    result["asphere_place"] = {"seconds": time.monotonic() - began, "status": placed.get("status"), "installed": placed.get("installed"),
                               "merit": daemon.backend.merit_value()}
    write_json(out / "rtc.json", result)

    _, ctx = session(start)
    began = time.monotonic()
    found = GlassExpert(ctx, codev["glass_surfaces"], catalogs=["SCHOTT"], neighbors=7, max_passes=5, max_seconds=budget,
                        candidate_filter=lambda _ctx, _surface, g: plain(g.name) in allowed).run()
    result["glass"] = {"seconds": time.monotonic() - began, "status": found["status"], "initial": found["initial_merit"], "final": found["final_merit"],
                       "trials": sum(e.get("phase") == "optimization" for e in found["events"]), "accepted": len(found["accepted"])}
    write_json(out / "rtc.json", result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("side", choices=("codev", "rtc"))
    parser.add_argument("--lens", type=Path, help="codev 侧的 .zmx")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget", type=float, default=600.0, help="每一步的上限（秒）")
    args = parser.parse_args()
    if args.side == "codev":
        if args.lens is None:
            parser.error("codev 需要 --lens")
        run_codev(args.lens.resolve(), args.output.resolve(), args.budget)
    else:
        run_rtc(args.output.resolve(), args.budget)


if __name__ == "__main__":
    main()
