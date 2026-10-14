"""saos 命令行。所有命令都向标准输出打印一个 JSON 对象：

    成功：{"ok": true, "result": ...}，退出码 0
    失败：{"ok": false, "error": {"code", "message", "details"}}，退出码 1

daemon 和 job 命令在本进程内执行；lens、opt、exec 命令转发给后台进程。配方的命令由各配方注册（saos microscope …）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from saos import client, jobs
from saos.protocol import ErrorCode, SaosError, fail, jsonable, ok
from saos.protocol import read_json as _read_json


def _lens_request(op: str, args: argparse.Namespace, payload: dict[str, Any]) -> Any:
    return client.request(op, {"job": args.job, **payload}, timeout=args.timeout)


def cmd_daemon(args: argparse.Namespace) -> Any:
    if args.action == "start":
        return client.start(args.mode, timeout=args.start_timeout)
    if args.action == "stop":
        return client.stop(discard=args.discard, force=args.force)
    if args.action == "status":
        return client.status()
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
            "spec": sorted(p.name for p in job.spec_dir.iterdir()),
            "log_tail": [json.loads(line) for line in lines[-args.log:]] if args.log > 0 else [],
        }
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
    if action in ("info", "surfaces", "view"):
        return _lens_request(f"lens.{action}", args, {})
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


def _surface_list(text: str) -> list[int]:
    """"1-16" 或 "1,3,5-7" → 面序号列表。"""
    out: list[int] = []
    for part in text.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        elif part.strip():
            out.append(int(part))
    return out


def cmd_opt(args: argparse.Namespace) -> Any:
    if args.action == "vary":
        payload = {"job": args.job, "param": args.param, "surfaces": _surface_list(args.surfaces), "on": not args.off}
        return client.request("opt.vary", payload, timeout=args.timeout)
    if args.action == "merit":
        payload: dict[str, Any] = {"job": args.job, "action": args.merit_action}
        if args.merit_action == "wizard":
            payload["settings"] = _read_json(args.file) if args.file else {}
        elif args.merit_action == "add":
            if not args.file:
                raise SaosError(ErrorCode.BAD_ARGS, "merit add 需要 --file 给出操作数列表 JSON")
            payload["rows"] = _read_json(args.file)
        return client.request("opt.merit", payload, timeout=args.timeout)
    if args.action == "local":
        return client.request("opt.local", {"job": args.job, "cycles": args.cycles, "cores": args.cores}, timeout=args.timeout)
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
    for item in args.arg:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise SaosError(ErrorCode.BAD_ARGS, f"--arg expects KEY=VALUE, got {item!r}")
        try:
            script_args[key] = json.loads(value)
        except ValueError:
            script_args[key] = value
    name = args.name or "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in script.stem)[:48] or "script"
    payload = {"job": args.job, "name": name, "source": source, "args": script_args, "quiet": args.quiet}
    return client.request("script.exec", payload, timeout=args.timeout)


class _Parser(argparse.ArgumentParser):
    """参数错误也按统一 JSON 格式输出，方便 agent 解析。"""

    def error(self, message: str) -> None:  # type: ignore[override]
        _emit(fail(SaosError(ErrorCode.BAD_ARGS, message, {"usage": self.format_usage().strip()})))
        sys.exit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="saos", description="由 agent 驱动 Zemax OpticStudio")
    sub = parser.add_subparsers(dest="group", required=True, parser_class=_Parser)

    daemon = sub.add_parser("daemon", help="后台进程")
    dsub = daemon.add_subparsers(dest="action", required=True, parser_class=_Parser)
    start = dsub.add_parser("start", help="启动后台进程并连接 OpticStudio")
    start.add_argument("--mode", choices=("standalone", "extension"), default="standalone")
    start.add_argument("--start-timeout", type=float, default=client.DEFAULT_START_TIMEOUT_S)
    stop = dsub.add_parser("stop", help="正常退出后台进程")
    stop.add_argument("--discard", action="store_true", help="放弃未保存的修改")
    stop.add_argument("--force", action="store_true", help="无法正常退出时强制结束进程")
    dsub.add_parser("status", help="查看后台进程状态")
    run = dsub.add_parser("run", help="在前台运行后台进程（由 start 调用，调试时也可直接用）")
    run.add_argument("--mode", choices=("standalone", "extension"), default="standalone")
    run.add_argument("--token", default="", help=argparse.SUPPRESS)
    daemon.set_defaults(func=cmd_daemon)

    job = sub.add_parser("job", help="设计任务目录")
    jsub = job.add_subparsers(dest="action", required=True, parser_class=_Parser)
    create = jsub.add_parser("create", help="新建设计任务")
    create.add_argument("--name", required=True)
    create.add_argument("--spec", help="复制到任务目录的光学规格文件")
    jsub.add_parser("list", help="列出设计任务")
    show = jsub.add_parser("show", help="查看设计任务")
    show.add_argument("job")
    show.add_argument("--log", type=int, default=10, help="显示最近几条执行记录")
    job.set_defaults(func=cmd_job)

    lens = sub.add_parser("lens", help="镜头操作（转发给后台进程）")
    lsub = lens.add_subparsers(dest="action", required=True, parser_class=_Parser)

    def lens_cmd(name: str, help_text: str) -> argparse.ArgumentParser:
        p = lsub.add_parser(name, help=help_text)
        p.add_argument("--job", required=True)
        p.add_argument("--timeout", type=float, default=client.DEFAULT_REQUEST_TIMEOUT_S)
        return p

    p = lens_cmd("import", "把外部 .zmx 复制到任务的 lenses/")
    p.add_argument("src")
    p.add_argument("--as", dest="as_name", help="在 lenses/ 中的文件名")
    p.add_argument("--overwrite", action="store_true")
    p = lens_cmd("capture", "将当前窗口内容另存为任务副本，保留原文件")
    p.add_argument("--as", dest="as_name", required=True)
    lens_cmd("view", "extension 模式下打开并刷新原生分析窗口")
    p = lens_cmd("load", "加载任务中的镜头文件")
    p.add_argument("file", help="相对 lenses/ 的文件名")
    p.add_argument("--discard", action="store_true", help="放弃当前镜头未保存的修改")
    p = lens_cmd("unload", "卸载当前镜头")
    p.add_argument("--discard", action="store_true")
    lens_cmd("info", "系统数据：孔径、视场、波长等")
    lens_cmd("surfaces", "镜头数据编辑器中的各面数据")
    p = lens_cmd("set", "修改一个面参数")
    p.add_argument("--surface", type=int, required=True)
    p.add_argument("--param", required=True, help="radius / thickness / conic / semi_diameter / material / comment")
    p.add_argument("--value", required=True, help="数值、inf，或材料名、注释文字")
    p = lens_cmd("save", "保存，默认覆盖当前文件并重新加载核对")
    p.add_argument("--as", dest="as_name", help="另存为 lenses/ 中的文件名")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--no-verify", action="store_true", help="不重新加载核对")
    p = lens_cmd("operand", "计算一个优化操作数的值")
    p.add_argument("type", help="操作数名，例如 EFFL")
    p.add_argument("--int1", type=int, default=0)
    p.add_argument("--int2", type=int, default=0)
    for i in range(1, 7):
        p.add_argument(f"--d{i}", type=float, default=0.0)
    p = lens_cmd("build", "按处方 JSON 搭建镜头并保存到任务的 lenses/")
    p.add_argument("prescription")
    p.add_argument("--as", dest="as_name", required=True)
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--discard", action="store_true")
    lens.set_defaults(func=cmd_lens)

    ex = sub.add_parser("exec", help="在后台进程内执行脚本：脚本定义 run(ctx)，副本和结果存进任务的 scripts/")
    ex.add_argument("script", help="Python 脚本路径")
    ex.add_argument("--job", required=True)
    ex.add_argument("--name", help="存档名，默认取脚本文件名")
    ex.add_argument("--arg", action="append", default=[], metavar="KEY=VALUE", help="传给 ctx.args；VALUE 按 JSON 解析，失败时当字符串")
    ex.add_argument("--args-file", help="JSON 对象文件，作为 ctx.args 的初值")
    ex.add_argument("--quiet", action="store_true", help="extension 模式下执行期间暂停窗口刷新")
    ex.add_argument("--timeout", type=float, default=client.DEFAULT_REQUEST_TIMEOUT_S)
    ex.set_defaults(func=cmd_exec)

    op = sub.add_parser("opt", help="优化（转发给后台进程）")
    osub = op.add_subparsers(dest="action", required=True, parser_class=_Parser)

    def opt_cmd(name: str, help_text: str) -> argparse.ArgumentParser:
        q = osub.add_parser(name, help=help_text)
        q.add_argument("--job", required=True)
        q.add_argument("--timeout", type=float, default=client.DEFAULT_REQUEST_TIMEOUT_S)
        return q

    q = opt_cmd("vary", "把面参数设为变量或取消变量")
    q.add_argument("--param", required=True, choices=("radius", "thickness", "conic"))
    q.add_argument("--surfaces", required=True, help="如 1-16 或 1,3,5-7")
    q.add_argument("--off", action="store_true", help="取消变量（固定）")
    q = opt_cmd("merit", "评价函数：clear / wizard / add / show")
    q.add_argument("merit_action", choices=("clear", "wizard", "add", "show"))
    q.add_argument("--file", help="wizard 的设置 JSON，或 add 的操作数列表 JSON")
    q = opt_cmd("local", "运行局部优化（阻尼最小二乘）")
    q.add_argument("--cycles", default="auto", choices=("auto", "1", "5", "10", "50"))
    q.add_argument("--cores", type=int)
    op.set_defaults(func=cmd_opt)

    from saos.recipes.microscope import cli as microscope_cli

    microscope_cli.register(sub, _Parser, client.DEFAULT_REQUEST_TIMEOUT_S)
    return parser


def _emit(message: dict[str, Any]) -> None:
    print(json.dumps(jsonable(message), ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except AttributeError:
        pass
    args = build_parser().parse_args(argv)
    if args.group == "daemon" and args.action == "run":
        from saos.daemon import serve

        return serve(args.mode, args.token)
    try:
        result = args.func(args)
    except SaosError as exc:
        _emit(fail(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        _emit(fail(SaosError(ErrorCode.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")))
        return 1
    _emit(ok(result))
    return 0
