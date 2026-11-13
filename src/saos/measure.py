"""本地的无状态测量：一份镜头文件进、JSON 出。在本进程新建的 rtc 实例上算，不经过后台进程的当前镜头，不占许可证、
不存证据，所以几个 agent 或子任务可以同时测不同的镜头，不会撞上 BUSY、UNSAVED_CHANGES 或 JOB_MISMATCH。
冷启动一次约 1.1 s（2026-11-13 本机实测：导入约 0.2 s，numba 从磁盘缓存加载追迹内核约 0.6 s，其余是追迹本身），
都是每个进程只付一次的固定开销，所以不另设常驻服务。rtc 读不了的镜头报 BACKEND_UNSUPPORTED，交给后台进程在
OpticStudio 上测。
Stateless local measurement: a lens file in, JSON out, computed on a fresh rtc instance in this process, outside the
daemon's current lens, with no licence and no evidence, so several agents can measure different lenses at once.
A cold call takes about 1.1 s, all one-off process costs (imports, numba loading the cached kernel), so there is no
resident service. A lens rtc cannot read is BACKEND_UNSUPPORTED: measure it through the daemon on OpticStudio."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from saos import config, jobs
from saos.protocol import ErrorCode, SaosError, finite, trust

KINDS = ("operand", "metric")


def _spec(check: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(check, dict) or len(check) != 1 or next(iter(check)) not in KINDS:
        raise SaosError(ErrorCode.BAD_ARGS, "each --check is one of {operand: {type, int1, int2, data}} or {metric: {id, params, pick}}, "
                        "as in a demand check; scripts run through saos exec, analyses through saos lens analysis",
                        {"got": check})
    kind, spec = next(iter(check.items()))
    if not isinstance(spec, dict) or ("type" if kind == "operand" else "id") not in spec:
        raise SaosError(ErrorCode.BAD_ARGS, f"{kind} needs {'type' if kind == 'operand' else 'id'}", {"got": check})
    return kind, spec


def _one(ctx, kind: str, spec: dict[str, Any]) -> Any:
    if kind == "operand":
        return ctx.backend.operand(str(spec["type"]), int(spec.get("int1", 0)), int(spec.get("int2", 0)),
                                   [float(x) for x in spec.get("data", [])])
    from saos import metrics

    return metrics.scalar(metrics.measure(ctx, spec["id"], **(spec.get("params") or {})), spec)


def run(lens: Path, checks: list[Any], *, job_root: Path | None = None, demand_ids: list[str] | None = None,
        demand: bool = False) -> dict[str, Any]:
    """在 lens 上测 checks（需求单 check 的 operand / metric 写法），demand 为真时另测 job_root 那个任务需求单的判定
    （demand_ids 为空测所有进 G 的项，与 check、round 同一套定义）。每项带 value、trust，用了未对账操作数的另带
    operands_unverified；rtc 算不了的项 trust 为 refused 并带 error。
    Measure checks (a demand check's operand / metric form) on lens and, with demand, the job's demand checks
    (demand_ids, or every item entering G). Each item has value and trust, operands_unverified when it used any;
    an item rtc cannot compute has trust refused and an error."""
    from saos.backend.rtc import RtcBackend, unreconciled
    from saos.parallel import WorkerContext

    lens = Path(lens)
    if not lens.is_file():
        raise SaosError(ErrorCode.FILE_NOT_FOUND, f"lens file not found: {lens}")
    specs = [_spec(c) for c in checks]
    if demand and job_root is None:
        raise SaosError(ErrorCode.BAD_ARGS, "--demand measures a job's demand checks; give --job")
    if not specs and not demand:
        raise SaosError(ErrorCode.BAD_ARGS, "give at least one --check, or --demand with --job")
    backend = RtcBackend(None, glass_dir=config.glass_dir(), standalone=True)
    try:
        backend.load(lens)
    except SaosError as exc:
        if exc.code == ErrorCode.BACKEND_UNSUPPORTED:
            exc.message += "; uv run saos backends --lens says why, and the daemon measures it on OpticStudio"
        raise
    paths = backend.unverified()
    with tempfile.TemporaryDirectory(prefix="saos-measure-") as scratch:
        ctx = WorkerContext(backend, Path(job_root) if job_root else Path(scratch), lens, 0)

        def graded(compute) -> dict[str, Any]:
            backend.take_used()
            try:
                value = compute()
            except SaosError as exc:
                if exc.code == ErrorCode.BACKEND_UNSUPPORTED:
                    return {"value": None, "trust": "refused", "error": exc.message}
                raise
            used = unreconciled(backend.take_used())
            return {"value": value, "trust": trust("rtc", {"unverified": paths, "operands_unverified": used}),
                    **({"operands_unverified": used} if used else {})}

        items = []
        for kind, spec in specs:
            out = graded(lambda: _one(ctx, kind, spec))
            items.append({"check": {kind: spec}, **out, "value": finite(out["value"])})
        result: dict[str, Any] = {"lens": str(lens), "computed_by": {**backend.identity(), "unverified": paths}, "items": items}
        if demand:
            from saos import demand as demand_mod

            d = demand_mod.load_job(Path(job_root))
            wanted = demand_ids or [s["id"] for s in demand_mod.checked(d) if s["goal"] is not None or "direction" in s]
            rows = []
            # 逐项单独测，才能按项分档；同一次 quick 内的缓存因此不跨项共用 / one quick per item, so each is graded on its own
            for item_id in dict.fromkeys(wanted):
                out = graded(lambda: demand_mod.quick(ctx, [item_id]))
                quick = out.pop("value") or {}
                error = (quick.get("errors") or {}).get(item_id)
                if error and (quick.get("error_kinds") or {}).get(item_id) == "backend":
                    out = {"trust": "refused"}
                elif error:
                    out = {}
                rows.append({"id": item_id, "value": finite((quick.get("values") or {}).get(item_id)), **out,
                             **({"error": error} if error else {})})
            result["demand"] = rows
    return result


def resolve(lens: str, job: str | None) -> tuple[Path, Path | None]:
    """--lens 是文件路径；给了 --job 时从该任务的 lenses/ 取。/ --lens is a path, or a file in the job's lenses/ with --job."""
    if job:
        j = jobs.get_job(job)
        return j.resolve_lens(lens, must_exist=True), j.root
    return Path(lens), None
