"""saos 命令行。所有命令都向标准输出打印一个 JSON 对象：

    成功：{"ok": true, "result": ...}，退出码 0
    失败：{"ok": false, "error": {"code", "message", "details"}}，退出码 1

daemon 和 job 命令在本进程内执行；lens 命令转发给后台进程。
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
    if action == "repro":
        return _lens_request("lens.repro", args, {"expect": _read_json(args.expect)})
    if action == "perturb":
        return _lens_request("lens.perturb", args, {"seed": args.seed, "radius_rel": args.radius_rel, "thickness_rel": args.thickness_rel})
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


def cmd_round(args: argparse.Namespace) -> Any:
    payload: dict[str, Any] = {"job": args.job, "note": args.note}
    if args.spec:
        payload["spec"] = args.spec
    return client.request("round.record", payload, timeout=args.timeout)


def cmd_eval(args: argparse.Namespace) -> Any:
    payload: dict[str, Any] = {"job": args.job}
    if args.spec:
        payload["spec"] = args.spec
    return client.request("eval.accept", payload, timeout=args.timeout)


def cmd_adapt(args: argparse.Namespace) -> Any:
    from saos import adapt

    extract = _read_json(args.extract)
    out = adapt.from_extraction(
        extract,
        embodiment=args.embodiment,
        focal_length_mm=args.focal_length,
        tube_lens_mm=args.tube_lens,
        field_number=args.field_number,
        catalogs=args.catalogs,
        stop=args.stop,
    )
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = {}
    for key, name in (("prescription", "prescription.json"), ("expect", "expect.json"), ("report", "adapt_report.json")):
        path = out_dir / name
        path.write_text(json.dumps(jsonable(out[key]), ensure_ascii=False, indent=2), encoding="utf-8")
        written[key] = str(path)
    return {"written": written, "expect": out["expect"], "report": out["report"]}


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
    p = lens_cmd("repro", "复现校验：焦距、数值孔径、工作距离、放大率与期望值比较")
    p.add_argument("expect", help="期望值 JSON")
    p = lens_cmd("perturb", "受控扰动：按固定随机种子改变物镜各面曲率和厚度")
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--radius-rel", type=float, required=True, help="曲率相对变化上限，如 0.01")
    p.add_argument("--thickness-rel", type=float, default=0.0)
    lens.set_defaults(func=cmd_lens)

    ev = sub.add_parser("eval", help="验收评估（转发给后台进程）")
    esub = ev.add_subparsers(dest="action", required=True, parser_class=_Parser)
    acc = esub.add_parser("accept", help="按光学规格对当前已保存的镜头做验收评估")
    acc.add_argument("--job", required=True)
    acc.add_argument("--spec", help="任务目录内的规格文件，默认用 job.json 中的 spec")
    acc.add_argument("--timeout", type=float, default=client.DEFAULT_REQUEST_TIMEOUT_S)
    ev.set_defaults(func=cmd_eval)

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

    rd = sub.add_parser("round", help="调整轮次（转发给后台进程）")
    rsub = rd.add_subparsers(dest="action", required=True, parser_class=_Parser)
    rec = rsub.add_parser("record", help="把当前镜头存为下一轮，做验收评估并给出是否停机")
    rec.add_argument("--job", required=True)
    rec.add_argument("--spec", help="第 0 轮必须给出；之后沿用，不能更换")
    rec.add_argument("--note", default="", help="本轮调整说明")
    rec.add_argument("--timeout", type=float, default=client.DEFAULT_REQUEST_TIMEOUT_S)
    rd.set_defaults(func=cmd_round)

    from saos import library_cli, plan_cli
    from saos.bench import cli as bench_cli

    library_cli.register(sub, _Parser, client.DEFAULT_REQUEST_TIMEOUT_S)
    bench_cli.register(sub, _Parser, client.DEFAULT_REQUEST_TIMEOUT_S)
    plan_cli.register(sub, _Parser)

    ad = sub.add_parser("adapt", help="把提取的面数据适配成标准设计布局的处方")
    asub = ad.add_subparsers(dest="action", required=True, parser_class=_Parser)
    pat = asub.add_parser("patent", help="专利/教材面数据提取结果 → 处方 + 复现期望值")
    pat.add_argument("extract", help="提取结果 JSON")
    pat.add_argument("--embodiment", help="schema 1 提取结果中的实施例键名")
    pat.add_argument("--focal-length", type=float, help="缩放到的物镜焦距 mm；默认毫米制按原值，归一化数据按 200/|β|")
    pat.add_argument("--tube-lens", type=float, default=200.0)
    pat.add_argument("--field-number", type=float, help="默认取提取结果中的视场数或像高")
    pat.add_argument("--catalogs", nargs="+", default=["OHARA", "SCHOTT", "HOYA", "SUMITA", "NIKON-HIKARI", "CDGM"])
    pat.add_argument("--stop", help="rear_focal、printed 或 r<面号>；默认有印出的光阑就用，否则 rear_focal")
    pat.add_argument("--out", required=True, help="输出目录")
    ad.set_defaults(func=cmd_adapt)
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
