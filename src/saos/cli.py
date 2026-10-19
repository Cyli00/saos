"""saos 命令行。所有命令都向标准输出打印一个 JSON 对象：

    成功：{"ok": true, "result": ...}，退出码 0
    失败：{"ok": false, "error": {"code", "message", "details"}}，退出码 1

daemon 和 job 命令在本进程内执行；lens、opt、exec 命令转发给后台进程。配方的命令由各配方注册（saos microscope …）。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

from saos import client, jobs
from saos.protocol import ErrorCode, SaosError, fail, jsonable, ok
from saos.protocol import read_json as _read_json


def _lens_request(op: str, args: argparse.Namespace, payload: dict[str, Any]) -> Any:
    return client.request(op, {"job": args.job, **payload}, timeout=args.timeout)


def _key_values(items: list[str], flag: str) -> dict[str, Any]:
    """KEY=VALUE 列表 → 字典；VALUE 按 JSON 解析，失败时当字符串。"""
    out: dict[str, Any] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise SaosError(ErrorCode.BAD_ARGS, f"{flag} expects KEY=VALUE, got {item!r}")
        try:
            out[key] = json.loads(value)
        except ValueError:
            out[key] = value
    return out


def cmd_daemon(args: argparse.Namespace) -> Any:
    if args.action == "start":
        return client.start(args.mode, timeout=args.start_timeout)
    if args.action == "stop":
        return client.stop(discard=args.discard, force=args.force)
    if args.action == "status":
        if args.no_env:
            return client.status()
        return {**client.status(), "environment": client.environment()}
    raise AssertionError(args.action)


def cmd_job(args: argparse.Namespace) -> Any:
    if args.action == "create":
        job = jobs.create_job(args.name, args.spec)
        return {"job": job.id, "path": str(job.root), "state": job.read_state()}
    if args.action == "list":
        return {"jobs": jobs.list_jobs()}
    if args.action == "show":
        job = jobs.get_job(args.job)
        lines = job.log_file.read_text(encoding="utf-8").splitlines() if job.log_file.is_file() else []
        return {
            "job": job.id,
            "path": str(job.root),
            "state": job.read_state(),
            "lenses": sorted(p.relative_to(job.lenses_dir).as_posix() for p in job.lenses_dir.rglob("*.zmx")),
            "spec": sorted(p.name for p in job.spec_dir.iterdir()) if job.spec_dir.is_dir() else [],
            "log_tail": [json.loads(line) for line in lines[-args.log:]] if args.log > 0 else [],
            # 直接读文件，后台进程正忙时也能看 / read from the file, so it works while the daemon is busy
            "progress": job.read_progress(),
            "cancel_requested": job.cancel_requested(),
        }
    if args.action == "cancel":
        return jobs.get_job(args.job).request_cancel()
    if args.action == "verify":
        job = jobs.get_job(args.job)
        problems = job.verify_seals()
        return {"job": job.id, "intact": not problems, "problems": problems}
    raise AssertionError(args.action)


def cmd_lens(args: argparse.Namespace) -> Any:
    action = args.action
    if action == "capture":
        return _lens_request("lens.capture", args, {"file": args.as_name})
    if action == "import":
        payload: dict[str, Any] = {"src": str(Path(args.src).resolve()), "overwrite": args.overwrite}
        if args.as_name:
            payload["name"] = args.as_name
        return _lens_request("lens.import", args, payload)
    if action == "load":
        return _lens_request("lens.load", args, {"file": args.file, "discard": args.discard})
    if action == "unload":
        return _lens_request("lens.unload", args, {"discard": args.discard})
    if action == "analysis":
        if args.list:
            return _lens_request("lens.analysis", args, {"list": True})
        if not args.type:
            raise SaosError(ErrorCode.BAD_ARGS, "analysis type is required unless --list is given")
        settings = _key_values(args.set, "--set")
        return _lens_request("lens.analysis", args, {"type": args.type, "settings": settings, "keep_open": args.keep_open})
    if action == "view":
        payload = {"viewer": args.viewer, "analysis": args.analysis}
        if args.set:
            payload["settings"] = _key_values(args.set, "--set")
        return _lens_request("lens.view", args, payload)
    if action == "screenshot":
        return _lens_request("lens.screenshot", args, {"label": args.label})
    if action in ("info", "surfaces"):
        return _lens_request(f"lens.{action}", args, {})
    if action == "describe":
        notes = _read_json(args.notes) if args.notes else None
        if notes is not None and not isinstance(notes, dict):
            raise SaosError(ErrorCode.BAD_ARGS, "--notes must contain a JSON object")
        return _lens_request("lens.describe", args, {"notes": notes, "lang": args.lang})
    if action == "approve":
        return _lens_request("lens.approve", args, {"model": args.model, "quote": args.quote})
    if action == "set":
        return _lens_request("lens.set", args, {"surface": args.surface, "param": args.param, "value": args.value})
    if action == "save":
        payload = {"verify": not args.no_verify, "overwrite": args.overwrite}
        if args.as_name:
            payload["file"] = args.as_name
        return _lens_request("lens.save", args, payload)
    if action == "operand":
        data = [getattr(args, f"d{i}") for i in range(1, 7)]
        while data and data[-1] == 0.0:
            data.pop()
        return _lens_request(
            "lens.operand", args, {"type": args.type, "int1": args.int1, "int2": args.int2, "data": data}
        )
    if action == "build":
        presc = _read_json(args.prescription)
        return _lens_request("lens.build", args, {"prescription": presc, "file": args.as_name, "overwrite": args.overwrite, "discard": args.discard})
    raise AssertionError(action)


# 一次 --surfaces 最多列出的面数，远多于任何镜头的面数 / the most surfaces one --surfaces may list, far beyond any lens
MAX_SURFACES = 10000


def _surface_list(text: str) -> list[int]:
    """"1-16" 或 "1,3,5-7" → 面序号列表；写错的段、倒序区间和空列表报 BAD_ARGS，不当作内部错误或悄悄变成空列表。"""
    out: list[int] = []
    for part in text.split(","):
        # 只认 ASCII 数字、最多 5 位：超长数字串会让 int() 抛异常，全角或其他文字的数字也不该当面号
        # ASCII digits only, at most 5: a huge digit string makes int() fail, and full-width or other scripts' digits are no surface numbers
        m = re.fullmatch(r"[ \t]*([0-9]{1,5})[ \t]*(?:-[ \t]*([0-9]{1,5})[ \t]*)?", part)
        if not m:
            raise SaosError(ErrorCode.BAD_ARGS, f"--surfaces takes surface numbers and ranges like 1,3,5-7; cannot read {part[:40]!r}")
        a, b = int(m[1]), int(m[2] or m[1])
        if a > b:
            raise SaosError(ErrorCode.BAD_ARGS, f"--surfaces range {a}-{b} runs backwards; write it as {b}-{a}")
        out.extend(range(a, b + 1))
        # 每段最多 5 位，单段至多十万个面号；总数再设上限，免得几段大区间在发往后台之前占满内存
        # at most 5 digits bound one range; the total is capped too, so wide ranges cannot fill memory before reaching the daemon
        if len(out) > MAX_SURFACES:
            raise SaosError(ErrorCode.BAD_ARGS, f"--surfaces lists more than {MAX_SURFACES} surfaces")
    return out


def cmd_opt(args: argparse.Namespace) -> Any:
    if args.action == "vary":
        payload = {"job": args.job, "param": args.param, "surfaces": _surface_list(args.surfaces), "on": not args.off,
                   "mode": args.mode, "catalog": args.catalog}
        return client.request("opt.vary", payload, timeout=args.timeout)
    if args.action == "merit":
        payload: dict[str, Any] = {"job": args.job, "action": args.merit_action}
        if args.merit_action == "wizard":
            payload["settings"] = _read_json(args.file) if args.file else {}
        elif args.merit_action == "add":
            if not args.file:
                raise SaosError(ErrorCode.BAD_ARGS, "merit add needs --file with a JSON list of operands")
            payload["rows"] = _read_json(args.file)
        return client.request("opt.merit", payload, timeout=args.timeout)
    if args.action == "local":
        return client.request("opt.local", {"job": args.job, "cycles": args.cycles, "cores": args.cores, "max_seconds": args.max_seconds}, timeout=args.timeout)
    if args.action in ("hammer", "global"):
        # 运行时长已知：客户端至少等到它结束后再留 2 分钟收尾 / the run length is known: wait for it plus 2 minutes to wrap up
        return client.request(f"opt.{args.action}", {"job": args.job, "cores": args.cores, "max_seconds": args.max_seconds},
                              timeout=max(args.timeout, args.max_seconds + 120))
    raise AssertionError(args.action)


def cmd_exec(args: argparse.Namespace) -> Any:
    script = Path(args.script)
    try:
        source = script.read_text(encoding="utf-8")
    except OSError as exc:
        raise SaosError(ErrorCode.FILE_NOT_FOUND, f"cannot read script {script}: {exc}") from exc
    script_args: dict[str, Any] = _read_json(args.args_file) if args.args_file else {}
    if not isinstance(script_args, dict):
        raise SaosError(ErrorCode.BAD_ARGS, "--args-file must contain a JSON object")
    script_args.update(_key_values(args.arg, "--arg"))
    name = args.name or "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in script.stem)[:48] or "script"
    payload = {"job": args.job, "name": name, "source": source, "args": script_args, "live": args.live}
    return client.request("script.exec", payload, timeout=args.timeout)


def cmd_check(args: argparse.Namespace) -> Any:
    payload: dict[str, Any] = {"job": args.job}
    if args.only:
        payload["only"] = [x.strip() for x in args.only.split(",") if x.strip()]
    return client.request("demand.check", payload, timeout=args.timeout)


def cmd_scan(args: argparse.Namespace) -> Any:
    if args.show is not None:
        from saos import scan

        return scan.show(jobs.get_job(args.job), None if args.show == "latest" else args.show)
    if not args.plan:
        raise SaosError(ErrorCode.BAD_ARGS, "give --plan to run a scan or --show [NNN] to read one")
    payload: dict[str, Any] = {"job": args.job, "plan": _read_json(args.plan), "detach": args.detach}
    if args.workers:
        payload["workers"] = args.workers
    return client.request("scan.run", payload, timeout=args.timeout)


def cmd_rule_out(args: argparse.Namespace) -> Any:
    from saos import demand, ledger

    root = jobs.get_job(args.job).root
    tiers = [t.strip() for item in args.tier for t in item.split(",") if t.strip()]
    return ledger.record_rule_out(root, demand.digest(demand.load_job(root)), tiers, args.reason)


def cmd_stop(args: argparse.Namespace) -> Any:
    from saos import demand, ledger

    root = jobs.get_job(args.job).root
    return ledger.record_stop(root, demand.digest(demand.load_job(root)), args.reason, args.quote)


def cmd_demand(args: argparse.Namespace) -> Any:
    from saos import demand

    root = jobs.get_job(args.job).root
    if args.action == "confirm":
        result = demand.confirm(root, args.quote)
        # 需求确认后确保流程图小窗口开着（已开着就不再开第二个）；SAOS_UI=0 时不开
        # make sure the workflow window is open once the demand is confirmed (no second one); not with SAOS_UI=0
        if os.environ.get("SAOS_UI") != "0":
            from saos import ui

            try:
                result["ui"] = ui.ensure_window(args.job, gate=True)
            except SaosError as exc:
                result["ui"] = {"error": exc.to_dict()}
        return result
    return demand.show(root)


def cmd_ui(args: argparse.Namespace) -> Any:
    from saos import ui

    if args.action == "notify":
        return ui.notify(args.job, args.message)
    return ui.open_window(args.job, window=not args.no_window)


def cmd_round(args: argparse.Namespace) -> Any:
    # 传输时 NaN 会变成 null、inf 变成字符串，所以发送前就要拒绝 / NaN would travel as null and inf as a string, so refuse them before sending
    if args.merit is not None and not math.isfinite(args.merit):
        raise SaosError(ErrorCode.BAD_ARGS, f"--merit must be a finite number (got {args.merit})")
    return client.request("round.record", {"job": args.job, "tier": args.tier, "note": args.note, "merit": args.merit, "viz": not args.no_viz},
                          timeout=args.timeout)


def cmd_report(args: argparse.Namespace) -> Any:
    from saos import report

    return report.write(jobs.get_job(args.job), lang=args.lang)


class _Parser(argparse.ArgumentParser):
    """参数错误也按统一 JSON 格式输出，方便 agent 解析。"""

    def error(self, message: str) -> None:  # type: ignore[override]
        _emit(fail(SaosError(ErrorCode.BAD_ARGS, message, {"usage": self.format_usage().strip()})))
        sys.exit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="saos", description="saos: tools for agents to drive Zemax OpticStudio")
    sub = parser.add_subparsers(dest="group", required=True, parser_class=_Parser)

    daemon = sub.add_parser("daemon", help="daemon holding the OpticStudio connection")
    dsub = daemon.add_subparsers(dest="action", required=True, parser_class=_Parser)
    start = dsub.add_parser("start", help="start the daemon and connect to OpticStudio")
    start.add_argument("--mode", choices=("standalone", "extension"), default="standalone")
    start.add_argument("--start-timeout", type=float, default=client.DEFAULT_START_TIMEOUT_S)
    stop = dsub.add_parser("stop", help="stop the daemon")
    stop.add_argument("--discard", action="store_true", help="discard unsaved changes")
    stop.add_argument("--force", action="store_true", help="kill the process if it does not exit normally")
    status = dsub.add_parser("status", help="show this instance's daemon and, once per session, the machine: daemons of every instance "
                             "and their running jobs, OpticStudio processes against the licence, visible windows, whether extension mode can start")
    status.add_argument("--no-env", action="store_true", help="only this instance's daemon")
    run = dsub.add_parser("run", help="run the daemon in the foreground (used by start; also for debugging)")
    run.add_argument("--mode", choices=("standalone", "extension"), default="standalone")
    run.add_argument("--token", default="", help=argparse.SUPPRESS)
    daemon.set_defaults(func=cmd_daemon)

    job = sub.add_parser("job", help="design job directories")
    jsub = job.add_subparsers(dest="action", required=True, parser_class=_Parser)
    create = jsub.add_parser("create", help="create a design job")
    create.add_argument("--name", required=True)
    create.add_argument("--spec", help="a spec file to copy into the job (microscope recipe)")
    jsub.add_parser("list", help="list design jobs")
    show = jsub.add_parser("show", help="show a design job, including the progress of a running computation (works while the daemon is busy)")
    show.add_argument("job")
    show.add_argument("--log", type=int, default=10, help="number of recent log records to show")
    verify = jsub.add_parser("verify", help="check that no evidence file written by the tools was changed or deleted (seals.jsonl)")
    verify.add_argument("job")
    cancel = jsub.add_parser("cancel", help="ask the running computation of a job to stop at its next safe point (opt local/hammer/global, scan, scripts that call ctx.progress); works while the daemon is busy")
    cancel.add_argument("job")
    job.set_defaults(func=cmd_job)

    lens = sub.add_parser("lens", help="lens operations (run by the daemon)")
    lsub = lens.add_subparsers(dest="action", required=True, parser_class=_Parser)

    def lens_cmd(name: str, help_text: str) -> argparse.ArgumentParser:
        p = lsub.add_parser(name, help=help_text)
        p.add_argument("--job", required=True)
        p.add_argument("--timeout", type=float, default=client.DEFAULT_REQUEST_TIMEOUT_S)
        return p

    p = lens_cmd("import", "copy an external .zmx into the job's lenses/")
    p.add_argument("src")
    p.add_argument("--as", dest="as_name", help="file name inside lenses/")
    p.add_argument("--overwrite", action="store_true")
    p = lens_cmd("capture", "save the current window content as a copy in the job, leaving the original file untouched")
    p.add_argument("--as", dest="as_name", required=True)
    p = lens_cmd("view", "explicit debugging: open Lens Data and a layout window, optionally one analysis, and refresh; reopens closed windows")
    p.add_argument("--viewer", choices=("cross-section", "3d", "shaded"),
                   help="layout window (default cross-section; a system it cannot draw, e.g. non-axial or non-sequential, gets the 3D viewer)")
    p.add_argument("--analysis", help="AnalysisIDM name of an analysis to keep open and refreshed (default none); pick what matters most for this system")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="settings of that analysis")
    p = lens_cmd("screenshot", "explicit debugging: save PNG screenshots of the OpticStudio windows to the job's screens/")
    p.add_argument("--label", default="window", help="file name label")
    p = lens_cmd("analysis", "run an OpticStudio analysis and export its numbers to the job's analyses/")
    p.add_argument("type", nargs="?", help="AnalysisIDM name, e.g. StandardSpot, FftMtf, RayFan, WavefrontMap")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="analysis setting, e.g. Field=all, Wavelength=1, SampleSize=S_64x64")
    p.add_argument("--keep-open", action="store_true", help="keep the analysis window open in extension mode")
    p.add_argument("--list", action="store_true", help="list the available analysis types")
    p = lens_cmd("load", "load a lens file of the job")
    p.add_argument("file", help="file name relative to lenses/")
    p.add_argument("--discard", action="store_true", help="discard unsaved changes of the current lens")
    p = lens_cmd("unload", "unload the current lens")
    p.add_argument("--discard", action="store_true")
    lens_cmd("info", "system data: aperture, fields, wavelengths, …")
    lens_cmd("surfaces", "surface rows of the Lens Data Editor")
    p = lens_cmd("describe", "model description page for the user (models/NNN-<lens>/index.html): every surface, material source and index per wavelength, apertures, fields, wavelengths, layout, plus automatic modelling checks")
    p.add_argument("--notes", help='JSON file: {"surfaces": {"<index>": "what this surface is"}, "assumptions": ["…"]}')
    p.add_argument("--lang", choices=("zh", "en"), help="page language (default: the demand lang, then SAOS_LANG, then the system language)")
    p = lens_cmd("approve", "record the user's approval of a model description; rounds need a lens whose structure matches an approved description")
    p.add_argument("--model", required=True, help="the description folder, e.g. models/001-start")
    p.add_argument("--quote", required=True, help="the user's own words of approval")
    p = lens_cmd("set", "change one surface parameter")
    p.add_argument("--surface", type=int, required=True)
    p.add_argument("--param", required=True, help="radius / thickness / conic / semi_diameter / material / comment / par<N> (a Lens Data parameter column)")
    p.add_argument("--value", required=True, help="a number, inf, a material name or comment text")
    p = lens_cmd("save", "save (default: overwrite the current file, then reload and compare)")
    p.add_argument("--as", dest="as_name", help="save as this file name inside lenses/")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--no-verify", action="store_true", help="skip the reload comparison")
    p = lens_cmd("operand", "evaluate one optimization operand")
    p.add_argument("type", help="operand name, e.g. EFFL")
    p.add_argument("--int1", type=int, default=0)
    p.add_argument("--int2", type=int, default=0)
    for i in range(1, 7):
        p.add_argument(f"--d{i}", type=float, default=0.0)
    p = lens_cmd("build", "build a lens from a prescription JSON and save it to the job's lenses/")
    p.add_argument("prescription")
    p.add_argument("--as", dest="as_name", required=True)
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--discard", action="store_true")
    lens.set_defaults(func=cmd_lens)

    ex = sub.add_parser("exec", help="run a script inside the daemon: it defines run(ctx); the copy and result are archived in the job's scripts/")
    ex.add_argument("script", help="path of the Python script")
    ex.add_argument("--job", required=True)
    ex.add_argument("--name", help="archive name (default: the script file name)")
    ex.add_argument("--arg", action="append", default=[], metavar="KEY=VALUE", help="passed in ctx.args; VALUE is parsed as JSON, else kept as a string")
    ex.add_argument("--args-file", help="JSON object file used as the initial ctx.args")
    ex.add_argument("--live", action="store_true", help="in extension mode, update the OpticStudio windows while the script runs (slower; by default they refresh once at the end)")
    ex.add_argument("--timeout", type=float, default=client.DEFAULT_REQUEST_TIMEOUT_S)
    ex.set_defaults(func=cmd_exec)

    op = sub.add_parser("opt", help="optimization (run by the daemon)")
    osub = op.add_subparsers(dest="action", required=True, parser_class=_Parser)

    def opt_cmd(name: str, help_text: str) -> argparse.ArgumentParser:
        q = osub.add_parser(name, help=help_text)
        q.add_argument("--job", required=True)
        q.add_argument("--timeout", type=float, default=client.DEFAULT_REQUEST_TIMEOUT_S)
        return q

    q = opt_cmd("vary", "make surface parameters variable or fixed")
    q.add_argument("--param", required=True, help="radius, thickness, conic, par<N> (a Lens Data parameter column: aspheric terms, tilts, …) or material")
    q.add_argument("--mode", choices=("model", "substitute"), help="for material: model = a model glass whose nd and Vd vary (exploration, not deliverable); substitute = OpticStudio swaps real glasses (Hammer / global search)")
    q.add_argument("--catalog", help="for --mode substitute: the catalog to substitute from (default: every catalog in use)")
    q.add_argument("--surfaces", required=True, help="e.g. 1-16 or 1,3,5-7")
    q.add_argument("--off", action="store_true", help="make them fixed")
    q = opt_cmd("merit", "merit function: clear / wizard / add / show")
    q.add_argument("merit_action", choices=("clear", "wizard", "add", "show"))
    q.add_argument("--file", help="wizard settings JSON, or the operand list JSON for add")
    q = opt_cmd("local", "run local optimization (damped least squares)")
    q.add_argument("--cycles", default="auto", choices=("auto", "1", "5", "10", "50"))
    q.add_argument("--cores", type=int, help="CPU cores for OpticStudio (default: all logical cores but 2)")
    q.add_argument("--max-seconds", type=float, help="cancel the optimisation after this many seconds and keep what it reached")
    for name, help_text in (("hammer", "run Hammer optimisation (escapes local minima; with material --mode substitute it swaps real glasses); "
                                       "keeps the best system found"),
                            ("global", "run global search; keeps the best system found and moves the ten best to lenses/global/NNN/")):
        q = opt_cmd(name, help_text)
        q.add_argument("--max-seconds", type=float, required=True, help="required: it never ends on its own; cancel after this many seconds")
        q.add_argument("--cores", type=int, help="CPU cores for OpticStudio (default: all logical cores but 2)")
    op.set_defaults(func=cmd_opt)

    dm = sub.add_parser("demand", help="the job's demand (sources/demand.yaml); no OpticStudio needed")
    dsub = dm.add_subparsers(dest="action", required=True, parser_class=_Parser)
    q = dsub.add_parser("show", help="validate the demand; print its version, confirmation, open questions and summary")
    q.add_argument("--job", required=True)
    q = dsub.add_parser("confirm", help="record the user's approval of the current demand version (bound to its content hash)")
    q.add_argument("--job", required=True)
    q.add_argument("--quote", required=True, help="the user's own words of approval")
    dm.set_defaults(func=cmd_demand)

    p = sub.add_parser("scan", help="concurrent scan, multi-start minimisation or perturbation probes on the daemon's worker pool: per point set parameters, optionally "
                                    "solve one variable, compensate, measure with the demand's checks; lens left unchanged; archived in scans/NNN/, "
                                    "the best lenses in lenses/scans/NNN/")
    p.add_argument("--job", required=True)
    p.add_argument("--plan", help="JSON plan: one of vars (grid), points, minimize or probes, plus fixed, solve, compensate, measure, operands, keep; see docs/cli.md")
    p.add_argument("--workers", type=int, default=None, help="OpticStudio instances to use (default the plan's workers, else 4; count the licence)")
    p.add_argument("--detach", action="store_true", help="return at once and run the scan in the background on the worker pool; the daemon keeps "
                                                         "serving other commands meanwhile; follow it with --show")
    p.add_argument("--show", nargs="?", const="latest", default=None, metavar="NNN",
                   help="read a scan's progress or result from its files (default the latest); works while the daemon is busy")
    p.add_argument("--timeout", type=float, default=client.DEFAULT_REQUEST_TIMEOUT_S)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("check", help="measure and judge every check of the demand (sources/demand.yaml) on the current lens without recording")
    p.add_argument("--job", required=True)
    p.add_argument("--only", help="comma-separated check ids to measure (exploration): the result is partial and never achieved; "
                                  "rounds always measure every check")
    p.add_argument("--timeout", type=float, default=client.DEFAULT_REQUEST_TIMEOUT_S)
    p.set_defaults(func=cmd_check)
    p = sub.add_parser("round", help="record a round: save lenses/rounds/rNNN.zmx, measure the demand, append ledger.jsonl and return the stop decision with a demand brief")
    p.add_argument("--job", required=True)
    p.add_argument("--tier", help="strategy tier(s) used in this round, comma-separated when several: local, variables, materials, global, structure; omit for round 0")
    p.add_argument("--note", default="", help="what this round changed")
    p.add_argument("--merit", type=float, default=None,
                   help="value of the objective you actually optimised, when you optimised outside the OpticStudio merit function; recorded instead of the lens's merit function value")
    p.add_argument("--no-viz", action="store_true", help="skip the spot / layout / MTF export for saos ui (evaluations/NNN/viz.json)")
    p.add_argument("--timeout", type=float, default=client.DEFAULT_REQUEST_TIMEOUT_S)
    p.set_defaults(func=cmd_round)
    p = sub.add_parser("rule-out", help="rule out strategy tiers that your evidence says cannot help, so the stop rule does not wait for them (holds until the next clear gain; no OpticStudio needed)")
    p.add_argument("--job", required=True)
    p.add_argument("--tier", action="append", required=True, help="tier(s) to rule out; repeat or comma-separate")
    p.add_argument("--reason", required=True, help="why the tier cannot help, citing the evidence (scans/NNN, a first-order result, a measurement)")
    p.set_defaults(func=cmd_rule_out)
    p = sub.add_parser("stop", help="record the user's decision to stop the rounds of the current demand version, with the reason and the user's words (no OpticStudio needed)")
    p.add_argument("--job", required=True)
    p.add_argument("--reason", required=True, help="why the rounds stop, with the evidence (e.g. scans/NNN, notes/findings.md F20)")
    p.add_argument("--quote", required=True, help="the user's own words deciding to stop, including the full text of any option they chose")
    p.set_defaults(func=cmd_stop)
    p = sub.add_parser("report", help="write the delivery report report/index.html from the demand and the ledger (no OpticStudio needed)")
    p.add_argument("--job", required=True)
    p.add_argument("--lang", choices=["en", "zh"], help="report language (default: demand lang, then SAOS_LANG, then the system UI language)")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("ui", help="open the workflow window for the user (flowchart, window controls in explicit debugging, per-round spot / layout / MTF); also opened by demand confirm")
    usub = p.add_subparsers(dest="action", required=True, parser_class=_Parser)
    q = usub.add_parser("open", help="start the local ui service if needed and open the window")
    q.add_argument("--job", required=True)
    q.add_argument("--no-window", action="store_true", help="only start the service and print the URL")
    q = usub.add_parser("notify", help="before you stop to ask the user: show a line at the top of the window and open or raise it")
    q.add_argument("--job", required=True)
    q.add_argument("--message", required=True, help="what the user should look at or answer, in their language")
    usub.add_parser("serve", help="run the ui service in the foreground (used by open)")
    p.set_defaults(func=cmd_ui)

    from saos.recipes.microscope import cli as microscope_cli

    microscope_cli.register(sub, _Parser, client.DEFAULT_REQUEST_TIMEOUT_S)
    return parser


def _emit(message: dict[str, Any]) -> None:
    print(json.dumps(jsonable(message), ensure_ascii=False, indent=2))


def _gate(args: argparse.Namespace, job: jobs.Job) -> bool:
    """需要用户看的节点：记一轮、建模说明、待确认的需求单。用户关掉的窗口在这些地方重新打开。
    Points where the user has to look: a round, a model description, a demand awaiting confirmation."""
    if args.func is cmd_round or (args.func is cmd_lens and args.action == "describe"):
        return True
    if args.func is cmd_demand and args.action == "show":
        from saos import demand

        try:
            return demand.confirmation_for(job.root, demand.digest(demand.load_job(job.root))) is None
        except SaosError:
            return True
    return False


def _ensure_window(args: argparse.Namespace) -> dict[str, Any] | None:
    """改动或计算镜头的带任务命令先确保用户的流程图窗口开着（saos.ui.ensure_window）；打不开不影响命令本身。
    Job commands that change or compute the lens first make sure the user's workflow window is open; failing to open it
    never fails the command."""
    if not getattr(args, "job", None):
        return None
    showing_demand = args.func is cmd_demand and args.action == "show"
    if args.func not in (cmd_lens, cmd_exec, cmd_opt, cmd_scan, cmd_check, cmd_round) and not showing_demand:
        return None
    if args.func is cmd_scan and args.show is not None:
        return None
    from saos import ui

    try:
        job = jobs.get_job(args.job)
        gate = _gate(args, job)
        if showing_demand and not gate:
            return None
        return ui.ensure_window(args.job, gate=gate)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except AttributeError:
        pass
    args = build_parser().parse_args(argv)
    if args.group == "daemon" and args.action == "run":
        from saos.daemon import serve

        return serve(args.mode, args.token)
    if args.group == "ui" and args.action == "serve":
        from saos.ui.server import serve as serve_ui

        return serve_ui()
    try:
        window = _ensure_window(args)
        result = args.func(args)
        if window is not None and isinstance(result, dict):
            result["ui_opened"] = window
    except SaosError as exc:
        _emit(fail(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        _emit(fail(SaosError(ErrorCode.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")))
        return 1
    _emit(ok(result))
    return 0
