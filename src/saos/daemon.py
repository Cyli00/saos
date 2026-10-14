"""常驻后台进程：持有 OpticStudio 连接，依次处理命令行发来的请求（ADR-0002）。

同一时间只加载一个任务的一个镜头文件。所有镜头操作都必须带上任务编号，
与当前加载的任务不一致时直接拒绝，保证不同设计任务的文件不会混用。
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import secrets
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from multiprocessing.connection import Connection, Listener
from pathlib import Path
from typing import Any, Callable

from saos import acceptance, config, jobs, prescription, rounds
from saos.backend import Backend, make_backend
from saos.backend.base import same_number
from saos.protocol import ErrorCode, SaosError, decode, encode, fail, jsonable, ok, write_json
from saos.spec import Spec, load_spec

log = logging.getLogger("saos.daemon")

REQUEST_READ_TIMEOUT_S = 30
COMPARED_TEXT_KEYS = ("type", "comment", "material", "is_stop", "solves")
COMPARED_NUMBER_KEYS = ("radius", "thickness", "conic", "semi_diameter")
# 连接手动打开的窗口时，这些操作期间暂停界面刷新，结束后窗口显示最终状态；SAOS_UI_LIVE=1 时全程刷新
QUIET_OPS = frozenset({"eval.accept", "round.record", "lens.repro"})


@dataclass
class Loaded:
    job: jobs.Job
    path: Path
    dirty: bool = False

    def describe(self) -> dict[str, Any]:
        return {"job": self.job.id, "file": self.job.relative_lens(self.path), "dirty": self.dirty}


def _arg(args: dict[str, Any], name: str, kind: type | tuple[type, ...], *, default: Any = ..., allow_none=False):
    if name not in args or (args[name] is None and not allow_none):
        if default is ...:
            raise SaosError(ErrorCode.BAD_ARGS, f"缺少参数 {name}")
        return default
    value = args[name]
    if value is None and allow_none:
        return None
    if kind is int and isinstance(value, bool) or not isinstance(value, kind):
        raise SaosError(ErrorCode.BAD_ARGS, f"参数 {name} 类型错误：{value!r}")
    return value


def compare_surfaces(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """保存前后的面数据逐项对比，返回不一致的条目。"""
    diffs = []
    if len(before) != len(after):
        return [{"field": "num_surfaces", "before": len(before), "after": len(after)}]
    for b, a in zip(before, after):
        for key in COMPARED_TEXT_KEYS:
            if b.get(key) != a.get(key):
                diffs.append({"surface": b["index"], "field": key, "before": b.get(key), "after": a.get(key)})
        for key in COMPARED_NUMBER_KEYS:
            x, y = float(b[key]), float(a[key])
            if not (same_number(x, y) or (math.isnan(x) and math.isnan(y))):
                diffs.append({"surface": b["index"], "field": key, "before": x, "after": y})
    return diffs


class Daemon:
    def __init__(self, backend: Backend, zos_info: dict[str, Any], mode: str):
        self.backend = backend
        self.zos_info = zos_info
        self.mode = mode
        # 启动时加载的代码版本；实验记录每轮核对，防止不同后台进程运行不同版本
        self.code_sha256 = config.code_sha256()
        self.started_at = time.time()
        self.loaded: Loaded | None = None
        self.stopping = False
        self.handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "ping": self.op_ping,
            "shutdown": self.op_shutdown,
            "lens.import": self.op_import,
            "lens.load": self.op_load,
            "lens.unload": self.op_unload,
            "lens.info": self.op_info,
            "lens.surfaces": self.op_surfaces,
            "lens.set": self.op_set,
            "lens.save": self.op_save,
            "lens.operand": self.op_operand,
            "lens.build": self.op_build,
            "lens.view": self.op_view,
            "lens.capture": self.op_capture,
            "lens.repro": self.op_repro,
            "eval.accept": self.op_accept,
            "lens.perturb": self.op_perturb,
            "opt.vary": self.op_vary,
            "opt.merit": self.op_merit,
            "opt.local": self.op_optimize,
            "round.record": self.op_round,
        }

    # ---- 请求分发 ----

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        op = request.get("op")
        args = request.get("args") or {}
        started = time.perf_counter()
        error: SaosError | None = None
        result: Any = None
        try:
            if not isinstance(args, dict):
                raise SaosError(ErrorCode.BAD_ARGS, "args 必须是对象")
            handler = self.handlers.get(op)  # type: ignore[arg-type]
            if handler is None:
                raise SaosError(ErrorCode.UNKNOWN_OP, f"未知操作 {op!r}", {"available": sorted(self.handlers)})
            if op not in ("ping", "shutdown") and not self.backend.healthy():
                raise self._connection_lost({"detected": "操作前的连接检查"})
            quiet = op in QUIET_OPS and os.environ.get("SAOS_UI_LIVE") != "1"
            with self.backend.activity(f"saos {op}", quiet):
                result = handler(args)
        except SaosError as exc:
            error = exc
        except Exception as exc:  # noqa: BLE001 - 后台进程不能因单个请求崩溃
            log.exception("操作 %s 出现未预期异常", op)
            error = self._unexpected(exc)
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        self._log_to_job(op, args, error, duration_ms)
        return fail(error) if error else ok(result)

    def _unexpected(self, exc: Exception) -> SaosError:
        detail = {"exception": type(exc).__name__, "message": str(exc)[:2000]}
        if self.backend.healthy():
            return SaosError(ErrorCode.ZOS_ERROR, f"后端操作失败：{exc}", detail)
        return self._connection_lost(detail)

    def _connection_lost(self, detail: dict[str, Any]) -> SaosError:
        # 实测（2024 R1）：OpticStudio 进程退出后，同一进程内无论复用还是新建 ZOSAPI_Connection，
        # CreateNewApplication 都报 RemotingException，无法重连。所以直接退出后台进程，由调用方重新启动。
        detail["lost_lens"] = self.loaded.describe() if self.loaded else None
        detail["daemon_exiting"] = True
        self.loaded = None
        self.stopping = True
        return SaosError(
            ErrorCode.ZOS_CONNECTION_LOST,
            "OpticStudio 连接已中断，后台进程随即退出；未保存的修改已丢失。执行 saos daemon start 重新启动后再加载镜头",
            detail,
        )

    def _log_to_job(self, op: Any, args: dict[str, Any], error: SaosError | None, duration_ms: float) -> None:
        job_id = args.get("job") if isinstance(args, dict) else None
        if not isinstance(job_id, str):
            return
        try:
            job = jobs.get_job(job_id)
        except SaosError:
            return
        record: dict[str, Any] = {"op": op, "args": args, "ok": error is None, "duration_ms": duration_ms}
        if error is not None:
            record["error"] = {"code": error.code, "message": error.message}
        try:
            job.append_log(record)
        except OSError:
            log.exception("写入任务 %s 的执行记录失败", job_id)

    # ---- 辅助 ----

    def _job(self, args: dict[str, Any]) -> jobs.Job:
        return jobs.get_job(_arg(args, "job", str))

    def _require_loaded(self, job: jobs.Job) -> Loaded:
        if self.loaded is None:
            raise SaosError(ErrorCode.NO_LENS_LOADED, f"任务 {job.id} 没有加载镜头，先执行 lens load")
        if self.loaded.job.id != job.id:
            raise SaosError(
                ErrorCode.JOB_MISMATCH,
                f"当前加载的是任务 {self.loaded.job.id} 的镜头，不能用任务 {job.id} 的名义操作",
                self.loaded.describe(),
            )
        return self.loaded

    def _check_not_dirty(self, discard: bool, action: str) -> None:
        if not discard and self.backend.has_unsaved_changes():
            raise SaosError(ErrorCode.UNSAVED_CHANGES, f"OpticStudio 窗口有未保存修改，{action}前先保存或捕获副本")
        if self.loaded is not None and self.loaded.dirty and not discard:
            raise SaosError(
                ErrorCode.UNSAVED_CHANGES,
                f"任务 {self.loaded.job.id} 的镜头有未保存的修改，{action}前先保存，或加 discard 放弃修改",
                self.loaded.describe(),
            )

    # ---- 操作 ----

    def op_ping(self, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "pid": os.getpid(),
            "mode": self.mode,
            "code_sha256": self.code_sha256,
            "started_at": datetime.fromtimestamp(self.started_at).astimezone().isoformat(timespec="seconds"),
            "uptime_s": round(time.time() - self.started_at, 1),
            "opticstudio": self.zos_info,
            "backend_healthy": self.backend.healthy(),
            "loaded": self.loaded.describe() if self.loaded else None,
            "home": str(config.saos_home()),
        }

    def op_shutdown(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check_not_dirty(bool(args.get("discard", False)), "退出")
        self.stopping = True
        return {"stopping": True, "discarded": self.loaded.describe() if self.loaded and self.loaded.dirty else None}

    def op_import(self, args: dict[str, Any]) -> dict[str, Any]:
        job = self._job(args)
        src = Path(_arg(args, "src", str))
        if not src.is_absolute():
            raise SaosError(ErrorCode.BAD_ARGS, "src 必须是绝对路径")
        if not src.is_file():
            raise SaosError(ErrorCode.FILE_NOT_FOUND, f"源镜头文件不存在：{src}")
        if src.suffix.lower() != jobs.LENS_SUFFIX:
            raise SaosError(ErrorCode.BAD_ARGS, f"只支持导入 {jobs.LENS_SUFFIX} 文件")
        name = _arg(args, "name", str, default=src.name)
        dest = job.resolve_lens(name, must_exist=False)
        if self.loaded is not None and self.loaded.path == dest:
            raise SaosError(ErrorCode.BAD_ARGS, f"{name} 正在加载中，不能用导入覆盖")
        if dest.exists() and not args.get("overwrite", False):
            raise SaosError(ErrorCode.FILE_EXISTS, f"任务 {job.id} 中已有 {name}，需要覆盖时加 overwrite")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return {"job": job.id, "file": job.relative_lens(dest), "source": str(src)}

    def op_load(self, args: dict[str, Any]) -> dict[str, Any]:
        job = self._job(args)
        path = job.resolve_lens(_arg(args, "file", str), must_exist=True)
        self._check_not_dirty(bool(args.get("discard", False)), "加载其他镜头")
        self.loaded = None
        self.backend.load(path)
        self.loaded = Loaded(job, path)
        rel = job.relative_lens(path)
        job.update_state(status="active", current_lens=rel)
        return {"job": job.id, "file": rel, "num_surfaces": len(self.backend.surfaces())}

    def op_unload(self, args: dict[str, Any]) -> dict[str, Any]:
        job = self._job(args)
        if self.loaded is None:
            return {"job": job.id, "unloaded": None}
        loaded = self._require_loaded(job)
        self._check_not_dirty(bool(args.get("discard", False)), "卸载")
        self.backend.new_system()
        self.loaded = None
        return {"job": job.id, "unloaded": loaded.describe()}

    def op_info(self, args: dict[str, Any]) -> dict[str, Any]:
        loaded = self._require_loaded(self._job(args))
        return {**loaded.describe(), "system": self.backend.system_info()}

    def op_surfaces(self, args: dict[str, Any]) -> dict[str, Any]:
        loaded = self._require_loaded(self._job(args))
        return {**loaded.describe(), "surfaces": self.backend.surfaces()}

    def op_set(self, args: dict[str, Any]) -> dict[str, Any]:
        loaded = self._require_loaded(self._job(args))
        surface = _arg(args, "surface", int)
        param = _arg(args, "param", str)
        if "value" not in args:
            raise SaosError(ErrorCode.BAD_ARGS, "缺少参数 value")
        current = self.backend.surfaces()
        before = current[surface].get(param) if 0 <= surface < len(current) else None
        after = self.backend.set_surface(surface, param, args["value"])
        loaded.dirty = True
        return {**loaded.describe(), "surface": surface, "param": param, "before": before, "after": after.get(param)}

    def op_save(self, args: dict[str, Any]) -> dict[str, Any]:
        job = self._job(args)
        loaded = self._require_loaded(job)
        name = _arg(args, "file", str, default=None)
        target = loaded.path if name is None else job.resolve_lens(name, must_exist=False)
        if target != loaded.path and target.exists() and not args.get("overwrite", False):
            raise SaosError(ErrorCode.FILE_EXISTS, f"任务 {job.id} 中已有 {name}，需要覆盖时加 overwrite")
        verify = bool(args.get("verify", True))
        snapshot = self.backend.surfaces() if verify else None
        target.parent.mkdir(parents=True, exist_ok=True)
        self.backend.save_as(target)
        loaded.path = target
        loaded.dirty = False
        rel = job.relative_lens(target)
        job.update_state(current_lens=rel, last_saved=rel)
        result: dict[str, Any] = {"job": job.id, "file": rel, "verified": False}
        if verify:
            # 从磁盘重新加载刚保存的文件，与保存前的内存数据逐项比对
            self.backend.load(target)
            diffs = compare_surfaces(snapshot or [], self.backend.surfaces())
            if diffs:
                raise SaosError(ErrorCode.VERIFY_FAILED, f"{rel} 重新加载后与保存前不一致", {"diffs": diffs[:50]})
            result.update(verified=True, surfaces_checked=len(snapshot or []))
        return result

    def op_build(self, args: dict[str, Any]) -> dict[str, Any]:
        job = self._job(args)
        presc = prescription.normalize(_arg(args, "prescription", dict))
        target = job.resolve_lens(_arg(args, "file", str), must_exist=False)
        if target.exists() and not args.get("overwrite", False):
            raise SaosError(ErrorCode.FILE_EXISTS, f"任务 {job.id} 中已有 {job.relative_lens(target)}，需要覆盖时加 overwrite")
        self._check_not_dirty(bool(args.get("discard", False)), "搭建新镜头")
        self.loaded = None
        self.backend.build(presc)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.backend.save_as(target)
        self.loaded = Loaded(job, target)
        rel = job.relative_lens(target)
        job.update_state(status="active", current_lens=rel, last_saved=rel)
        return {"job": job.id, "file": rel, "num_surfaces": len(self.backend.surfaces())}

    def op_capture(self, args: dict[str, Any]) -> dict[str, Any]:
        job = self._job(args)
        if self.loaded is not None:
            self._require_loaded(job)
        target = job.resolve_lens(_arg(args, "file", str), must_exist=False)
        if target.exists():
            raise SaosError(ErrorCode.FILE_EXISTS, "捕获副本不能覆盖已有文件")
        target.parent.mkdir(parents=True, exist_ok=True)
        original = self.backend.system_info()["file"]
        self.backend.save_as(target)
        self.loaded = Loaded(job, target)
        rel = job.relative_lens(target)
        job.update_state(current_lens=rel, last_saved=rel, captured_from=original)
        return {"file": rel, "captured_from": original}

    def op_view(self, args: dict[str, Any]) -> dict[str, Any]:
        loaded = self._require_loaded(self._job(args))
        return {**loaded.describe(), **self.backend.show_analyses()}

    def op_repro(self, args: dict[str, Any]) -> dict[str, Any]:
        loaded = self._require_loaded(self._job(args))
        expect = _arg(args, "expect", dict)
        design = self.backend.snapshot()
        gap = prescription.gap_surface(design)
        original = design[gap]["thickness"]
        primary = next(w["um"] for w in self.backend.system_info()["wavelengths"] if w["primary"])
        try:
            result = acceptance.reproduction_check(self.backend, design, expect, primary)
        except acceptance.InvalidEvaluation as exc:
            raise SaosError(ErrorCode.ZOS_ERROR, f"复现校验无法完成：{exc.reason}", exc.details) from exc
        finally:
            # 复现校验只临时调焦，结束后恢复，不改变已加载的镜头。未修改的镜头直接重新加载文件：
            # 实测调焦用的 ZERN 和写回原厚度都会让 OpticStudio 的 NeedsSave 置位，extension 模式下会误报未保存修改
            if loaded.dirty:
                self.backend.set_surface(gap, "thickness", original)
            else:
                self.backend.load(loaded.path)
        return {**loaded.describe(), **result}

    def op_accept(self, args: dict[str, Any]) -> dict[str, Any]:
        job = self._job(args)
        loaded = self._require_loaded(job)
        if loaded.dirty:
            raise SaosError(ErrorCode.UNSAVED_CHANGES, "验收评估针对已保存的镜头文件，先保存", loaded.describe())
        spec_name = _arg(args, "spec", str, default=None) or job.read_state().get("spec")
        if not spec_name:
            raise SaosError(ErrorCode.BAD_ARGS, f"任务 {job.id} 没有光学规格，用 --spec 指定或在 job create 时给出")
        return self._accept(job, loaded, spec_name, self._load_job_spec(job, spec_name))

    @staticmethod
    def _load_job_spec(job: jobs.Job, spec_name: str) -> Spec:
        spec_path = (job.root / spec_name).resolve()
        if job.root.resolve() not in spec_path.parents or not spec_path.is_file():
            raise SaosError(ErrorCode.PATH_OUTSIDE_JOB, f"规格文件必须在任务目录内：{spec_name}")
        return load_spec(spec_path)

    def _accept(self, job: jobs.Job, loaded: Loaded, spec_name: str, spec: Spec) -> dict[str, Any]:
        design = self.backend.snapshot()
        info = self.backend.system_info()
        evaluator = acceptance.Evaluator(
            self.backend, spec, info["material_catalogs"], telecentric_object=info["aperture"].get("telecentric_object", False)
        )
        try:
            result = evaluator.run(design)
        finally:
            stem = loaded.path.stem
            eval_dir = job.root / "eval"
            eval_dir.mkdir(exist_ok=True)
            model_path = eval_dir / f"{stem}.accept.zmx"
            try:
                self.backend.save_as(model_path)
            except Exception:  # noqa: BLE001 - 搭建失败时可能没有可保存的模型
                model_path = None
            self.backend.load(loaded.path)
        rel = job.relative_lens(loaded.path)
        result = {"job": job.id, "file": rel, "spec": spec_name, **result}
        if model_path is not None:
            result["acceptance_model"] = model_path.relative_to(job.root).as_posix()
        write_json(job.root / "eval" / f"{loaded.path.stem}.accept.json", result)
        job.update_state(last_evaluation={"file": rel, "verdict": result["verdict"], "spec_shortfall": jsonable(result["spec_shortfall"])})
        return result

    # ---- 优化与调整轮次 ----

    def op_perturb(self, args: dict[str, Any]) -> dict[str, Any]:
        """受控扰动（固定随机种子）：物镜各面曲率按相对量、各面厚度按相对量均匀随机改变。"""
        loaded = self._require_loaded(self._job(args))
        seed = _arg(args, "seed", int)
        radius_rel = float(_arg(args, "radius_rel", (int, float)))
        thickness_rel = float(_arg(args, "thickness_rel", (int, float), default=0.0))
        rows = self.backend.snapshot()
        first, last = prescription.objective_block(rows)
        rng = random.Random(seed)
        changes = []
        for r in rows[first : last + 1]:
            if r["type"] != "standard" or r["comment"] == prescription.TAG_STOP:
                continue
            i = r["index"]
            if not math.isinf(r["radius"]):
                new_r = 1.0 / ((1.0 / r["radius"]) * (1.0 + rng.uniform(-radius_rel, radius_rel)))
                self.backend.set_surface(i, "radius", new_r)
                changes.append({"surface": i, "param": "radius", "before": r["radius"], "after": new_r})
            if i < last and thickness_rel > 0:
                new_t = r["thickness"] * (1.0 + rng.uniform(-thickness_rel, thickness_rel))
                self.backend.set_surface(i, "thickness", new_t)
                changes.append({"surface": i, "param": "thickness", "before": r["thickness"], "after": new_t})
        loaded.dirty = True
        return {**loaded.describe(), "seed": seed, "radius_rel": radius_rel, "thickness_rel": thickness_rel, "changes": changes}

    def op_vary(self, args: dict[str, Any]) -> dict[str, Any]:
        loaded = self._require_loaded(self._job(args))
        param = _arg(args, "param", str)
        surfaces = _arg(args, "surfaces", list)
        on = bool(args.get("on", True))
        solves = {int(s): self.backend.vary(int(s), param, on) for s in surfaces}
        loaded.dirty = True
        return {**loaded.describe(), "param": param, "solves": solves}

    def op_merit(self, args: dict[str, Any]) -> dict[str, Any]:
        loaded = self._require_loaded(self._job(args))
        action = _arg(args, "action", str)
        if action == "clear":
            self.backend.merit_clear()
        elif action == "wizard":
            self.backend.merit_wizard(_arg(args, "settings", dict, default={}))
        elif action == "add":
            self.backend.merit_add(_arg(args, "rows", list))
        elif action != "show":
            raise SaosError(ErrorCode.BAD_ARGS, f"未知的评价函数操作 {action}")
        if action != "show":
            loaded.dirty = True
        return {**loaded.describe(), "merit": self.backend.merit_value(), "rows": self.backend.merit_rows()}

    def op_optimize(self, args: dict[str, Any]) -> dict[str, Any]:
        loaded = self._require_loaded(self._job(args))
        result = self.backend.optimize(str(args.get("cycles", "auto")), args.get("cores"))
        loaded.dirty = True
        return {**loaded.describe(), **result}

    def op_round(self, args: dict[str, Any]) -> dict[str, Any]:
        """记录一个调整轮次：把当前镜头存为 rounds/rNNN.zmx，做验收评估，给出是否停机。

        observe=True 用于校准停机规则（saos bench run）：照常冻结并核对验收配置，
        但停机判断只记录、不执行，轨迹可以走到停机之后。"""
        job = self._job(args)
        loaded = self._require_loaded(job)
        log_path = job.root / "rounds.jsonl"
        history = rounds.load(log_path)
        frozen = job.read_state().get("frozen") if history else None
        spec_name = _arg(args, "spec", str, default=None) or (frozen or {}).get("spec")
        if not spec_name:
            raise SaosError(ErrorCode.BAD_ARGS, "第 0 轮必须用 --spec 指定光学规格，之后各轮沿用")
        spec = self._load_job_spec(job, spec_name)
        config = acceptance.acceptance_config(spec)
        digest = acceptance.config_hash(config)
        if history:
            # 每轮都按当前规格文件重新补全，与第 0 轮冻结的完整验收配置逐项比较
            rounds.check_frozen(job, spec_name, config, digest)
            status = rounds.decide([float(h["spec_shortfall"]) for h in history])["status"]
            if status != "continue" and not args.get("observe", False):
                raise SaosError(ErrorCode.BAD_ARGS, f"调整已停止（{status}），不能继续记录轮次", {"status": status})
        else:
            rounds.freeze(job, spec_name, config, digest)
        number = len(history)
        target = job.resolve_lens(f"rounds/r{number:03d}.zmx", must_exist=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        merit = self.backend.merit_value()
        self.backend.save_as(target)
        loaded.path, loaded.dirty = target, False
        result = self._accept(job, loaded, spec_name, spec)
        record = {
            "round": number,
            "file": job.relative_lens(target),
            "verdict": result["verdict"],
            "spec_shortfall": result["spec_shortfall"],
            "items": {i["id"]: i["shortfall"] for i in result["items"] if i["shortfall"] > 0},
            "merit": merit,
            "note": str(args.get("note", "")),
            "observe": bool(args.get("observe", False)),
            "config_sha256": digest,
            "time": jobs.now_iso(),
        }
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(jsonable(record), ensure_ascii=False) + "\n")
        decision = rounds.decide([float(h["spec_shortfall"]) for h in history] + [float(result["spec_shortfall"])])
        job.update_state(current_lens=record["file"], last_saved=record["file"], round=number, round_status=decision["status"])
        return {"record": record, "decision": decision, "evaluation": result}

    def op_operand(self, args: dict[str, Any]) -> dict[str, Any]:
        loaded = self._require_loaded(self._job(args))
        name = _arg(args, "type", str)
        int1 = _arg(args, "int1", int, default=0)
        int2 = _arg(args, "int2", int, default=0)
        data = _arg(args, "data", list, default=[])
        value = self.backend.operand(name, int1, int2, data)
        return {**loaded.describe(), "type": name.upper(), "int1": int1, "int2": int2, "data": data, "value": value}


# ---- 进程生命周期 ----


def _serve_one(daemon: Daemon, conn: Connection) -> None:
    if not conn.poll(REQUEST_READ_TIMEOUT_S):
        return
    try:
        request = decode(conn.recv_bytes())
        response = daemon.handle(request)
    except SaosError as exc:
        response = fail(exc)
    conn.send_bytes(encode(response))


def serve(mode: str, token: str = "") -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except AttributeError:
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ddir = config.daemon_dir()
    ddir.mkdir(parents=True, exist_ok=True)
    error_file = ddir / "start_error.json"
    error_file.unlink(missing_ok=True)

    backend = make_backend(mode)
    log.info("后台进程 %s 启动，连接 OpticStudio（%s）", os.getpid(), mode)
    try:
        zos_info = backend.start()
    except SaosError as exc:
        log.error("启动失败：%s", exc.message)
        write_json(error_file, exc.to_dict())
        return 1
    except Exception as exc:  # noqa: BLE001
        log.exception("启动失败")
        write_json(error_file, SaosError(ErrorCode.DAEMON_START_FAILED, str(exc)).to_dict())
        return 1

    authkey = secrets.token_bytes(32)
    try:
        # AF_PIPE 以“首个实例”方式创建命名管道，同名管道已存在时会失败，防止重复启动
        listener = Listener(config.pipe_address(), family="AF_PIPE", authkey=authkey)
    except OSError as exc:
        backend.close()
        write_json(error_file, SaosError(ErrorCode.DAEMON_ALREADY_RUNNING, f"命名管道已被占用：{exc}").to_dict())
        return 1

    config.daemon_authkey_file().write_bytes(authkey)
    daemon = Daemon(backend, zos_info, mode)
    write_json(
        config.daemon_state_file(),
        {
            "pid": os.getpid(),
            "token": token,
            "address": config.pipe_address(),
            "mode": mode,
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "opticstudio": zos_info,
        },
    )
    log.info("就绪：%s，OpticStudio %s", config.pipe_address(), zos_info.get("version"))
    try:
        while not daemon.stopping:
            try:
                conn = listener.accept()
            except Exception as exc:  # noqa: BLE001 - 认证失败等只影响单个连接
                log.warning("拒绝连接：%s", exc)
                continue
            try:
                _serve_one(daemon, conn)
            except Exception:  # noqa: BLE001
                log.exception("处理请求时出错")
            finally:
                conn.close()
    finally:
        log.info("正在退出")
        backend.close()
        listener.close()
        config.daemon_state_file().unlink(missing_ok=True)
        config.daemon_authkey_file().unlink(missing_ok=True)
        log.info("已退出")
    return 0
