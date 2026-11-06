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

from saos import client, jobs, lens_files
from saos.protocol import ErrorCode, SaosError, fail, jsonable, ok
from saos.protocol import read_json as _read_json


def _lens_request(op: str, args: argparse.Namespace, payload: dict[str, Any]) -> Any:
    return client.request(op, {"job": args.job, **payload}, timeout=args.timeout)


def _send(op: str, args: argparse.Namespace, payload: dict[str, Any], timeout: float) -> Any:
    """--detach 时交给分离子进程、立即返回 runs/ 下的结果文件名；否则照常等待。
    With --detach hand the request to a detached child and return the runs/ result file at once; otherwise wait as usual."""
    if getattr(args, "detach", False):
        return client.detach(op, payload, jobs.get_job(payload["job"]).root)
    return client.request(op, payload, timeout=timeout)


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
        return client.start(timeout=args.start_timeout, backend=args.backend)
    if args.action == "stop":
        if args.all:
            return client.stop_all(discard=args.discard, force=args.force)
        return client.stop(discard=args.discard, force=args.force)
    if args.action == "list":
        found = client.all_daemons()
        return {"daemons": found if args.all else [d for d in found if d["current"]]}
    if args.action == "status":
        if args.no_env:
            return client.status()
        return {**client.status(), "environment": client.environment()}
    if args.action == "use":
        return client.request("daemon.use", {"backend": args.backend, "job": args.job}, timeout=args.timeout)
    raise AssertionError(args.action)


def cmd_job(args: argparse.Namespace) -> Any:
    if args.action == "create":
        job = jobs.create_job(args.name, args.mode)
        return {"job": job.id, "path": str(job.root), "state": job.read_state()}
    if args.action == "list":
        return {"jobs": jobs.list_jobs()}
    if args.action == "show":
        from saos.ui import stage

        job = jobs.get_job(args.job)
        lines = job.log_file.read_text(encoding="utf-8").splitlines() if job.log_file.is_file() else []
        snapshot = stage.snapshot(job)
        return {
            "job": job.id,
            "path": str(job.root),
            "state": job.read_state(),
            # 工作流走到哪一步，与流程窗口同源 / where the job stands in the workflow, as the workflow window shows it
            # revisions：用户在窗口里对当前版本提出、还没出新版的修改意见 / change requests on the current versions, not yet answered
            "workflow": {k: snapshot[k] for k in ("mode", "current", "steps", "blocking", "decision", "revisions")},
            "lenses": sorted(p.relative_to(job.lenses_dir).as_posix() for p in lens_files.all_lenses(job.lenses_dir)),
            "log_tail": [json.loads(line) for line in lines[-args.log:]] if args.log > 0 else [],
            # 直接读文件，后台进程正忙时也能看 / read from the file, so it works while the daemon is busy
            "progress": job.read_progress(),
            "cancel_requested": job.cancel_requested(),
            # --detach 的运行：状态和结果文件 / runs started with --detach: status and result file
            "runs": client.detached_runs(job.root),
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
        return _lens_request("lens.analysis", args, {"type": args.type, "settings": settings})
    if action in ("info", "surfaces"):
        return _lens_request(f"lens.{action}", args, {})
    if action == "describe":
        notes = _read_json(args.notes) if args.notes else None
        if notes is not None and not isinstance(notes, dict):
            raise SaosError(ErrorCode.BAD_ARGS, "--notes must contain a JSON object")
        return _lens_request("lens.describe", args, {"notes": notes, "lang": args.lang})
    if action == "approve":
        from saos import ui

        via = "auto" if args.reason else "cli"
        if via == "cli":
            ui.require_window_closed(args.job, "approve")
        return _lens_request("lens.approve", args, {"model": args.model, "quote": args.quote or "", "rebuild_reason": args.rebuild_reason,
                                                    "via": via, "reason": args.reason or ""})
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


def _surface_list(text: str, flag: str = "--surfaces") -> list[int]:
    """"1-16" 或 "1,3,5-7" → 面序号列表；写错的段、倒序区间和空列表报 BAD_ARGS，不当作内部错误或悄悄变成空列表。"""
    out: list[int] = []
    for part in text.split(","):
        # 只认 ASCII 数字、最多 5 位：超长数字串会让 int() 抛异常，全角或其他文字的数字也不该当面号
        # ASCII digits only, at most 5: a huge digit string makes int() fail, and full-width or other scripts' digits are no surface numbers
        m = re.fullmatch(r"[ \t]*([0-9]{1,5})[ \t]*(?:-[ \t]*([0-9]{1,5})[ \t]*)?", part)
        if not m:
            raise SaosError(ErrorCode.BAD_ARGS, f"{flag} takes numbers and ranges like 1,3,5-7; cannot read {part[:40]!r}")
        a, b = int(m[1]), int(m[2] or m[1])
        if a > b:
            raise SaosError(ErrorCode.BAD_ARGS, f"{flag} range {a}-{b} runs backwards; write it as {b}-{a}")
        out.extend(range(a, b + 1))
        # 每段最多 5 位，单段至多十万个面号；总数再设上限，免得几段大区间在发往后台之前占满内存
        # at most 5 digits bound one range; the total is capped too, so wide ranges cannot fill memory before reaching the daemon
        if len(out) > MAX_SURFACES:
            raise SaosError(ErrorCode.BAD_ARGS, f"{flag} lists more than {MAX_SURFACES} entries")
    return out


def cmd_opt(args: argparse.Namespace) -> Any:
    if args.action == "vary":
        if args.config_rows:
            if args.param or args.surfaces or args.objects:
                raise SaosError(ErrorCode.BAD_ARGS, "give one of --param with --surfaces, --param with --objects, or --config-rows")
            payload = {"job": args.job, "config_rows": _surface_list(args.config_rows, "--config-rows"),
                       "configs": _surface_list(args.configs, "--configs") if args.configs else None, "on": not args.off}
            return client.request("opt.vary", payload, timeout=args.timeout)
        if args.objects:
            # 非序列物体的参数 / parameters of non-sequential objects
            if not args.param or args.surfaces:
                raise SaosError(ErrorCode.BAD_ARGS, "opt vary --objects needs --param and no --surfaces")
            payload = {"job": args.job, "param": args.param, "objects": _surface_list(args.objects, "--objects"), "group": args.group,
                       "on": not args.off}
            return client.request("opt.vary", payload, timeout=args.timeout)
        if not (args.param and args.surfaces):
            raise SaosError(ErrorCode.BAD_ARGS, "opt vary needs --param and --surfaces, --param and --objects, or --config-rows")
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
        return _send("opt.local", args, {"job": args.job, "cycles": args.cycles, "cores": args.cores, "max_seconds": args.max_seconds,
                                         "measure": args.measure, "algorithm": args.algorithm}, args.timeout)
    if args.action in ("hammer", "global"):
        # 运行时长已知：客户端至少等到它结束后再留 2 分钟收尾 / the run length is known: wait for it plus 2 minutes to wrap up
        payload = {"job": args.job, "cores": args.cores, "max_seconds": args.max_seconds, "measure": args.measure}
        if getattr(args, "rel", None) is not None:
            payload["rel"] = args.rel
        return _send(f"opt.{args.action}", args, payload, max(args.timeout, args.max_seconds + 120))
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
    payload = {"job": args.job, "name": name, "source": source, "args": script_args}
    return _send("script.exec", args, payload, args.timeout)


def cmd_check(args: argparse.Namespace) -> Any:
    payload: dict[str, Any] = {"job": args.job}
    if args.only:
        payload["only"] = [x.strip() for x in args.only.split(",") if x.strip()]
    return _send("demand.check", args, payload, args.timeout)


def cmd_scan(args: argparse.Namespace) -> Any:
    if not args.job:
        raise SaosError(ErrorCode.BAD_ARGS, "give --job J")
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
    d = demand.load_job(root)
    return ledger.record_rule_out(root, demand.compute_digest(d), tiers, args.reason, d)


def cmd_stop(args: argparse.Namespace) -> Any:
    from saos import demand, ledger

    root = jobs.get_job(args.job).root
    d = demand.load_job(root)
    # 不带 --quote 的停止只在自主任务里可以（record_stop 核对）/ a stop without --quote: autonomous jobs only
    return ledger.record_stop(root, demand.compute_digest(d), args.reason, args.quote or "", d, via="cli" if args.quote else "auto")


def cmd_demand(args: argparse.Namespace) -> Any:
    from saos import demand

    root = jobs.get_job(args.job).root
    if args.action == "confirm":
        from saos import ui

        if args.reason:
            result = demand.confirm(root, args.quote or "", via="auto", reason=args.reason)
            return result
        ui.require_window_closed(args.job, "confirm")
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
    chosen = ui.configure(jobs.get_job(args.job), tabs=args.tabs, metrics=args.metrics, reset=args.reset)
    return {**ui.open_window(args.job, window=not args.no_window, tab=args.tab), "ui": chosen}


def cmd_round(args: argparse.Namespace) -> Any:
    # 传输时 NaN 会变成 null、inf 变成字符串，所以发送前就要拒绝 / NaN would travel as null and inf as a string, so refuse them before sending
    if args.merit is not None and not math.isfinite(args.merit):
        raise SaosError(ErrorCode.BAD_ARGS, f"--merit must be a finite number (got {args.merit})")
    if args.confirm:
        if args.tier or args.note or args.merit is not None:
            raise SaosError(ErrorCode.BAD_ARGS, "--confirm re-measures a recorded round; it takes --of, not --tier, --note or --merit")
        return _send("round.confirm", args, {"job": args.job, "of": args.of, "viz": not args.no_viz, "discard": args.discard}, args.timeout)
    if args.of is not None or args.discard:
        raise SaosError(ErrorCode.BAD_ARGS, "--of and --discard go with --confirm")
    return _send("round.record", args, {"job": args.job, "tier": args.tier, "note": args.note, "merit": args.merit, "viz": not args.no_viz},
                 args.timeout)


def cmd_report(args: argparse.Namespace) -> Any:
    from saos import report

    return report.write(jobs.get_job(args.job), lang=args.lang)


class _Parser(argparse.ArgumentParser):
    """参数错误也按统一 JSON 格式输出，方便 agent 解析。"""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", argparse.RawDescriptionHelpFormatter)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:  # type: ignore[override]
        _emit(fail(SaosError(ErrorCode.BAD_ARGS, message, {"usage": self.format_usage().strip()})))
        sys.exit(2)


def cmd_metrics(args: argparse.Namespace) -> Any:
    payload: dict[str, Any] = {}
    if args.action == "get":
        payload.update(id=args.id, params=_key_values(args.param, "--param"))
    elif args.action == "select":
        payload["items"] = _read_json(Path(args.file))
    return _lens_request(f"metrics.{args.action}", args, payload)


def build_parser() -> argparse.ArgumentParser:
    # 命令与子命令的说明只写在 cli_help（COMMANDS / GROUPS），由 cli_help.configure 填入 / command help lives only in cli_help
    parser = _Parser(prog="saos")
    sub = parser.add_subparsers(dest="group", required=True, parser_class=_Parser)

    metric_group = sub.add_parser("metrics")
    metric_sub = metric_group.add_subparsers(dest="action", required=True, parser_class=_Parser)
    for action in ("list", "get", "select"):
        metric_parser = metric_sub.add_parser(action)
        metric_parser.add_argument("--job", required=True)
        metric_parser.add_argument("--timeout", type=float, default=client.DEFAULT_REQUEST_TIMEOUT_S)
        metric_parser.set_defaults(func=cmd_metrics)
        if action == "get":
            metric_parser.add_argument("id", help="metric catalog id")
            metric_parser.add_argument("--param", action="append", default=[], help="parameter KEY=JSON; repeat for more")
        elif action == "select":
            metric_parser.add_argument("--file", required=True, help="JSON list of {key,id,params?,pick?}; [] clears display extras")

    daemon = sub.add_parser("daemon")
    dsub = daemon.add_subparsers(dest="action", required=True, parser_class=_Parser)
    start = dsub.add_parser("start")
    start.add_argument("--backend", choices=("auto", "zos", "rtc"), default=None,
                       help="auto (default, or SAOS_BACKEND): rtc at once, OpticStudio started when first needed; zos: both, "
                            "OpticStudio connected at start; rtc: rtc only, no OpticStudio")
    start.add_argument("--start-timeout", type=float, default=client.DEFAULT_START_TIMEOUT_S)
    stop = dsub.add_parser("stop")
    stop.add_argument("--discard", action="store_true", help="discard unsaved changes")
    stop.add_argument("--force", action="store_true", help="kill the process if it does not exit normally")
    stop.add_argument("--all", action="store_true", help="stop every daemon on the machine (all instances under work/ and registered ones elsewhere) "
                      "and clear leftover state files of dead ones")
    lst = dsub.add_parser("list")
    lst.add_argument("--all", action="store_true", help="every daemon on the machine, not only this instance's")
    status = dsub.add_parser("status")
    status.add_argument("--no-env", action="store_true", help="only this instance's daemon")
    use = dsub.add_parser("use")
    use.add_argument("backend", choices=("auto", "rtc", "zos"), help="auto: by workflow stage; rtc or zos: pin it")
    use.add_argument("--job", default=None, help="hand this job's loaded lens over now")
    use.add_argument("--timeout", type=float, default=client.DEFAULT_REQUEST_TIMEOUT_S)
    run = dsub.add_parser("run")
    run.add_argument("--backend", choices=("auto", "zos", "rtc"), default=None, help=argparse.SUPPRESS)
    run.add_argument("--token", default="", help=argparse.SUPPRESS)
    daemon.set_defaults(func=cmd_daemon)

    job = sub.add_parser("job")
    jsub = job.add_subparsers(dest="action", required=True, parser_class=_Parser)
    create = jsub.add_parser("create")
    create.add_argument("--name", required=True)
    create.add_argument("--mode", choices=jobs.MODES, default="interactive",
                        help="interactive: the user confirms the gates in the window and may ask for changes (designing from scratch); "
                             "autonomous: the agent passes the gates with --reason and records its decisions (exercises, tests)")
    jsub.add_parser("list")
    show = jsub.add_parser("show")
    show.add_argument("job")
    show.add_argument("--log", type=int, default=10, help="number of recent log records to show")
    verify = jsub.add_parser("verify")
    verify.add_argument("job")
    cancel = jsub.add_parser("cancel")
    cancel.add_argument("job")
    job.set_defaults(func=cmd_job)

    lens = sub.add_parser("lens")
    lsub = lens.add_subparsers(dest="action", required=True, parser_class=_Parser)

    def lens_cmd(name: str) -> argparse.ArgumentParser:
        p = lsub.add_parser(name)
        p.add_argument("--job", required=True)
        p.add_argument("--timeout", type=float, default=client.DEFAULT_REQUEST_TIMEOUT_S)
        return p

    p = lens_cmd("import")
    p.add_argument("src")
    p.add_argument("--as", dest="as_name", help="file name inside lenses/")
    p.add_argument("--overwrite", action="store_true")
    p = lens_cmd("analysis")
    p.add_argument("type", nargs="?", help="AnalysisIDM name, e.g. StandardSpot, FftMtf, RayFan, WavefrontMap")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="analysis setting, e.g. Field=all, Wavelength=1, SampleSize=S_64x64")
    p.add_argument("--list", action="store_true", help="list the available analysis types")
    p = lens_cmd("load")
    p.add_argument("file", help="file name relative to lenses/")
    p.add_argument("--discard", action="store_true", help="discard unsaved changes of the current lens")
    p = lens_cmd("unload")
    p.add_argument("--discard", action="store_true")
    lens_cmd("info")
    lens_cmd("surfaces")
    p = lens_cmd("describe")
    p.add_argument("--notes", help="path to the model notes JSON file: surfaces or objects, environment, assumptions, first_order and exploration; format below")
    p.add_argument("--lang", choices=("zh", "en"), help="page language (default: the demand lang, then SAOS_LANG, then the system language)")
    p = lens_cmd("approve")
    p.add_argument("--model", required=True, help="the description folder, e.g. models/001-start")
    p.add_argument("--quote", default=None, help="the user's own words of approval (fallback when the window cannot be used)")
    p.add_argument("--reason", default=None, help="autonomous jobs only: why this structure is the one to design")
    p.add_argument("--rebuild-reason", default="", help="required for structural or environment-declaration changes under the round gate comparison; appended samples are allowed")
    p = lens_cmd("set")
    p.add_argument("--surface", type=int, required=True)
    p.add_argument("--param", required=True, help="radius / thickness / conic / semi_diameter / mech_semi_diameter (the part's outer edge, "
                                                   "used by the interference check) / material / comment / par<N> (a Lens Data parameter column)")
    p.add_argument("--value", required=True, help="a number, inf, a material name or comment text")
    p = lens_cmd("save")
    p.add_argument("--as", dest="as_name", help="save as this file name inside lenses/")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--no-verify", action="store_true", help="skip the reload comparison")
    p = lens_cmd("operand")
    p.add_argument("type", help="operand name, e.g. EFFL")
    p.add_argument("--int1", type=int, default=0)
    p.add_argument("--int2", type=int, default=0)
    for i in range(1, 7):
        p.add_argument(f"--d{i}", type=float, default=0.0)
    p = lens_cmd("build")
    p.add_argument("prescription")
    p.add_argument("--as", dest="as_name", required=True)
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--discard", action="store_true")
    lens.set_defaults(func=cmd_lens)

    ex = sub.add_parser("exec")
    ex.add_argument("script", help="path of the Python script")
    ex.add_argument("--job", required=True)
    ex.add_argument("--name", help="archive name (default: the script file name)")
    ex.add_argument("--arg", action="append", default=[], metavar="KEY=VALUE", help="passed in ctx.args; VALUE is parsed as JSON, else kept as a string")
    ex.add_argument("--args-file", help="JSON object file used as the initial ctx.args")
    ex.add_argument("--detach", action="store_true", help="return at once; a detached process waits for the daemon and writes the response to the job's runs/NNN-<op>.json (saos job show lists it). Use it for anything that may outlast your own command time limit")
    ex.add_argument("--timeout", type=float, default=client.DEFAULT_REQUEST_TIMEOUT_S)
    ex.set_defaults(func=cmd_exec)

    op = sub.add_parser("opt")
    osub = op.add_subparsers(dest="action", required=True, parser_class=_Parser)

    def opt_cmd(name: str) -> argparse.ArgumentParser:
        q = osub.add_parser(name)
        q.add_argument("--job", required=True)
        q.add_argument("--timeout", type=float, default=client.DEFAULT_REQUEST_TIMEOUT_S)
        return q

    q = opt_cmd("vary")
    q.add_argument("--param", help="radius, curvature (the same variable as radius; bounds in 1/mm), thickness, conic, par<N> "
                                   "(a Lens Data parameter column: aspheric terms, tilts, …), material，或 rtc 浮动孔径光阑的 semi_diameter（内存变量）")
    q.add_argument("--mode", choices=("model", "substitute"), help="for material: model = a model glass whose nd and Vd vary (exploration, not deliverable); substitute = OpticStudio swaps real glasses (Hammer / global search)")
    q.add_argument("--catalog", help="for --mode substitute: the catalog to substitute from (default: SAOS_CDGM_INPROD)")
    q.add_argument("--surfaces", help="e.g. 1-16 or 1,3,5-7")
    q.add_argument("--config-rows", help="multi-configuration rows instead of surfaces (zoom spacings, focus, …), e.g. 1,3")
    q.add_argument("--configs", help="with --config-rows: the configurations (default: all), e.g. 2-3")
    q.add_argument("--objects", help="non-sequential objects instead of surfaces, e.g. 2-4; --param is x, y, z, tilt_x, tilt_y, tilt_z, par<N> or a column header")
    q.add_argument("--group", type=int, help="with --objects in a sequential lens: the NonSequential surface holding them (default: the only one)")
    q.add_argument("--off", action="store_true", help="make them fixed")
    q = opt_cmd("merit")
    q.add_argument("merit_action", choices=("clear", "wizard", "add", "show"))
    q.add_argument("--file", help="wizard settings JSON, or the operand list JSON for add")
    q = opt_cmd("local")
    q.add_argument("--cycles", default="auto", choices=("auto", "1", "5", "10", "50"))
    q.add_argument("--algorithm", choices=("auto", "dls", "od"), help="auto selects DLS for sequential systems and orthogonal descent for non-sequential systems")
    q.add_argument("--cores", type=int, help="CPU cores: OpticStudio threads, or rtc worker processes for hammer and global (default: all logical cores but 2)")
    q.add_argument("--max-seconds", type=float, help="finite positive optimisation budget in seconds; "
                        "includes measurements and review; an incomplete review returns completed=false and valid=false. Native calls may overrun")
    for name in ("hammer", "global"):
        q = opt_cmd(name)
        q.add_argument("--max-seconds", type=float, required=True, help="required: it never ends on its own; cancel after this many seconds")
        q.add_argument("--cores", type=int, help="CPU cores: OpticStudio threads, or rtc worker processes for hammer and global (default: all logical cores but 2)")
        q.add_argument("--rel", type=float, default=None, help="stop early at a checkpoint when the relative merit gain since the previous checkpoint is below rel")
    for name in ("local", "hammer", "global"):
        q = osub.choices[name]
        q.add_argument("--measure", help="measure these demand checks before and after the run and list what got better or worse: "
                                         "comma-separated ids, hard (constants and variable ranges) or all; pick fast checks")
        q.add_argument("--detach", action="store_true", help="return at once; a detached process waits for the daemon and writes the response to the job's runs/NNN-<op>.json (saos job show lists it). Use it for anything that may outlast your own command time limit")
    op.set_defaults(func=cmd_opt)

    dm = sub.add_parser("demand")
    dsub = dm.add_subparsers(dest="action", required=True, parser_class=_Parser)
    q = dsub.add_parser("show")
    q.add_argument("--job", required=True)
    q = dsub.add_parser("confirm")
    q.add_argument("--job", required=True)
    q.add_argument("--quote", default=None, help="the user's own words of approval (fallback when the window cannot be used)")
    q.add_argument("--reason", default=None, help="autonomous jobs only: why this demand is fit to work on")
    dm.set_defaults(func=cmd_demand)

    p = sub.add_parser("scan")
    p.add_argument("--job", default=None, help="job id (required for running and --show)")
    p.add_argument("--plan", help="JSON plan file: one of vars (grid), points, minimize, probes, candidates (structure exploration) or tolerance, "
                                  "plus fixed, solve, compensate, measure, operands, keep; formats and examples below")
    p.add_argument("--workers", type=int, default=None, help="worker processes to use (default the plan's workers, else 4 standalone OpticStudio instances; subject to available licences)")
    p.add_argument("--detach", action="store_true", help="return at once and run the scan in the background on the worker pool; the daemon keeps "
                                                         "serving other commands meanwhile; follow it with --show")
    p.add_argument("--show", nargs="?", const="latest", default=None, metavar="NNN",
                   help="read a scan's progress or result from its files (default the latest); works while the daemon is busy")
    p.add_argument("--timeout", type=float, default=client.DEFAULT_REQUEST_TIMEOUT_S)
    p.set_defaults(func=cmd_scan)
    p = sub.add_parser("check")
    p.add_argument("--job", required=True)
    p.add_argument("--only", help="comma-separated check ids to measure (exploration): the result is partial and never achieved; "
                                  "rounds always measure every check")
    p.add_argument("--detach", action="store_true", help="return at once; a detached process waits for the daemon and writes the response to the job's runs/NNN-<op>.json (saos job show lists it). Use it for anything that may outlast your own command time limit")
    p.add_argument("--timeout", type=float, default=client.DEFAULT_REQUEST_TIMEOUT_S)
    p.set_defaults(func=cmd_check)
    p = sub.add_parser("round")
    p.add_argument("--job", required=True)
    p.add_argument("--tier", help="strategy tier(s): local, variables, materials, global, structure; omit for round 0")
    p.add_argument("--note", default="", help="what this round changed")
    p.add_argument("--merit", type=float, default=None,
                   help="value of the objective you actually optimised, when you optimised outside the OpticStudio merit function; recorded instead of the lens's merit function value")
    p.add_argument("--no-viz", action="store_true", help="skip the layout / spot / ray fan / field / MTF export for saos ui (evaluations/NNN/viz.json)")
    p.add_argument("--confirm", action="store_true", help="on an OpticStudio instance: re-measure a round recorded on the rtc instance before delivery")
    p.add_argument("--of", type=int, default=None, help="with --confirm: the round to re-measure (default: the round saos report would deliver)")
    p.add_argument("--discard", action="store_true", help="with --confirm: discard unsaved changes of the current lens, as lens load --discard")
    p.add_argument("--detach", action="store_true", help="return at once; a detached process waits for the daemon and writes the response to the job's runs/NNN-<op>.json (saos job show lists it). Use it for anything that may outlast your own command time limit")
    p.add_argument("--timeout", type=float, default=client.DEFAULT_REQUEST_TIMEOUT_S)
    p.set_defaults(func=cmd_round)
    p = sub.add_parser("rule-out")
    p.add_argument("--job", required=True)
    p.add_argument("--tier", action="append", required=True, help="tier(s) to rule out; repeat or comma-separate")
    p.add_argument("--reason", required=True, help="why the tier cannot help, citing the evidence (scans/NNN, a first-order result, a measurement)")
    p.set_defaults(func=cmd_rule_out)
    p = sub.add_parser("stop")
    p.add_argument("--job", required=True)
    p.add_argument("--reason", required=True, help="why the rounds stop, with the evidence (e.g. scans/NNN, notes/findings.md F20)")
    p.add_argument("--quote", default=None, help="the user's own words deciding to stop, including the full text of any option they chose; "
                                                  "an autonomous job may stop without it")
    p.set_defaults(func=cmd_stop)
    p = sub.add_parser("report")
    p.add_argument("--job", required=True)
    p.add_argument("--lang", choices=["en", "zh"], help="report language (default: demand lang, then SAOS_LANG, then the system UI language)")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("ui")
    usub = p.add_subparsers(dest="action", required=True, parser_class=_Parser)
    q = usub.add_parser("open")
    q.add_argument("--job", required=True)
    q.add_argument("--no-window", action="store_true", help="only start the service and print the URL")
    q.add_argument("--tabs", help="comma-separated analysis tabs to show, in order: spot,fan,field,mtf,detectors,metrics,rounds,starts "
                                  "(remembered for the job; the layout always has its own card, so layout is accepted and ignored)")
    q.add_argument("--metrics", help="comma-separated demand item ids the Metrics tab shows, in order (default every checked item; remembered for the job)")
    q.add_argument("--tab", help="tab to show first when the window opens")
    q.add_argument("--reset", action="store_true", help="forget the job's tab and metric choices (back to the default tabs)")
    q = usub.add_parser("notify")
    q.add_argument("--job", required=True)
    q.add_argument("--message", required=True, help="what the user should look at or answer, in their language")
    usub.add_parser("serve")
    p.set_defaults(func=cmd_ui)

    from saos.recipes.microscope import cli as microscope_cli

    microscope_cli.register(sub, _Parser, client.DEFAULT_REQUEST_TIMEOUT_S)
    from saos import cli_help

    cli_help.configure(parser)
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
            d = demand.load_job(job.root)
            return demand.confirmation_for(job.root, demand.digest(d), d) is None
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

        if args.backend:
            # 后台进程和它的 worker 都按这个后端启动 / the daemon and its workers start on this backend
            os.environ["SAOS_BACKEND"] = args.backend
        return serve(args.token)
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
