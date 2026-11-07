"""Expert 共用的镜头事务、预算与 ZOS 优化入口；不持有第二个计算后端。"""
from __future__ import annotations

import math
import tempfile
from pathlib import Path

from saos.budget import Budget, Stop
from saos.protocol import ErrorCode, SaosError


class Rejected(Exception):
    """已完成计算但不满足物理条件；与后端/API 失效分开处理。"""


def number(value, name, *, positive=False, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SaosError(ErrorCode.BAD_ARGS, f"{name} must be a finite number")
    if (positive and value <= 0) or (nonnegative and value < 0):
        raise SaosError(ErrorCode.BAD_ARGS, f"{name} must be {'positive' if positive else 'nonnegative'}")
    return float(value)


def integer(value, name, minimum=1):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SaosError(ErrorCode.BAD_ARGS, f"{name} must be an integer >= {minimum}")
    return value


def editable(ctx):
    if getattr(ctx, "in_check", False):
        raise SaosError(ErrorCode.BAD_ARGS, "check scripts are read-only: experts are not available in a check")


def sequential(ctx):
    info = ctx.backend.system_info()
    # rtc 实例（standalone，ADR-0017）自己持有镜头，可以跑；ctx.fast 的副本不行 / the rtc instance owns its lens; an rtc copy does not
    if info.get("backend") == "rtc" and not getattr(ctx.backend, "standalone", False):
        raise SaosError(ErrorCode.BACKEND_UNSUPPORTED, "experts require the authoritative lens, not an rtc exploration copy")
    if info.get("mode") == "non_sequential" or info.get("nsc_groups"):
        raise SaosError(ErrorCode.BACKEND_UNSUPPORTED, "experts currently require a purely sequential lens")
    if ctx.backend.configuration_count() != 1:
        # OpticStudio 上要插 MCE 行才能让新系数跟随温度，这条路径还没有真机验证
        # on OpticStudio new terms need MCE rows to follow the temperature, and that path has not been run on the real tool
        if info.get("backend") != "rtc":
            raise SaosError(ErrorCode.BACKEND_UNSUPPORTED, "on OpticStudio experts require one configuration; run them on the rtc instance")
        _nominal_configuration(ctx)
    return info


def _nominal_configuration(ctx):
    """跟随格只能在它跟随的结构上改，所以专家类要从那个结构开始。"""
    current = ctx.backend.current_configuration()
    rows = ctx.backend.lens.mce
    code = ctx.backend.lens.thermal_code
    sources = {r.source_config for r in rows if r.solve_code == code}
    if sources and current not in sources:
        raise SaosError(ErrorCode.BAD_ARGS, f"configuration {current} follows configuration {sorted(sources)[0]}; select that one before running an expert")


def covered(ctx):
    """多结构时评价函数必须覆盖每个结构，否则专家类只在其中一个结构上比较，其余结构的退化看不到。
    On several configurations the merit must cover each, or an expert compares on one and cannot see the others degrade."""
    count = ctx.backend.configuration_count()
    if count == 1:
        return
    rows = ctx.backend.merit
    first = next((k for k, r in enumerate(rows) if r.type == "CONF"), len(rows))
    seen = {r.int1 for r in rows if r.type == "CONF"}
    if any(r.weight > 0 and r.type != "CONF" for r in rows[:first]):
        seen.add(ctx.backend.current_configuration())
    missing = sorted(set(range(1, count + 1)) - seen)
    if missing:
        raise SaosError(ErrorCode.BAD_ARGS, f"the merit function does not cover configurations {missing}; build it with saos opt merit wizard "
                                            "or add CONF rows before running an expert")


def references(ctx, refs):
    if isinstance(refs, (str, int)):
        refs = [refs]
    indices = [ctx.surface(ref) for ref in refs]
    count = len(ctx.backend.surfaces())
    if not indices or len(set(indices)) != len(indices) or any(not 1 <= i < count-1 for i in indices):
        raise SaosError(ErrorCode.BAD_ARGS, "expert surfaces must be unique real surfaces before the image")
    return indices


def merit(ctx):
    value = float(ctx.backend.merit_value())
    if not math.isfinite(value) or value < 0:
        raise Rejected("merit is not finite and nonnegative")
    return value


def feasible(check, ctx, surface):
    if check is not None:
        result = check(ctx, surface)
        if not isinstance(result, bool):
            raise SaosError(ErrorCode.BAD_ARGS, "feasible(ctx, surface) must return bool; missing data is not acceptance")
        if not result:
            raise Rejected("feasibility check rejected the current design")


def optimize(ctx, callback, budget, *, cycles="auto", cores=None, algorithm="auto"):
    budget.check()
    if callback is None:
        result = ctx.backend.optimize(cycles, cores, max_seconds=budget.remaining(),
                                     should_cancel=budget.should_cancel, algorithm=algorithm)
    else:
        result = callback(ctx)
    budget.check()
    if isinstance(result, dict):
        if result.get("stopped_by"):
            budget.stopped_by = result["stopped_by"]
            raise Stop
        if result.get("solver_succeeded") is False:
            raise Rejected("local optimizer failed")
    if callback is None:
        # 在 OpticStudio 上按评价函数优化过，算作 rtc 结果的收尾 / an OpticStudio optimisation on the merit closes rtc results
        note = getattr(ctx, "_note_rtc", None)
        if note is not None:
            note(False)
    return merit(ctx)


class Session:
    """完整镜头快照包含求解、MFE 与面型；异常回入口，成功显式提交最后接受态。

    与 ctx.restored 一样直接使用当前 backend 的 save_as/load，不改变 daemon 的镜头归属。
    保存快照会改变 ZOS SystemFile；调用方仍须通过 ctx.save() 保存，不能调用原生 Save()。
    """
    def __init__(self, ctx, seconds, *, rollback=True):
        self.ctx, self.rollback = ctx, rollback
        self.budget = Budget(seconds, getattr(ctx, "cancel_requested", None))
        self.loaded = getattr(getattr(ctx, "_daemon", None), "loaded", None)
        self.was_dirty = getattr(self.loaded, "dirty", None)
        self.serial = 0
        self.changed = False
        self.unclosed = {}  # 快照 → 当时 rtc 结果是否未收尾，恢复快照时一并恢复 / per snapshot, restored with it

    def __enter__(self):
        editable(self.ctx)
        if getattr(self.ctx, "_expert_active", False):
            raise SaosError(ErrorCode.BAD_ARGS, "nested expert runs on one context are not supported")
        self.ctx._expert_active = True
        self.directory = tempfile.TemporaryDirectory(prefix="saos-expert-")
        try:
            self.entry = self.save()
        except BaseException:
            self.directory.cleanup()
            self.ctx._expert_active = False
            raise
        return self

    def save(self):
        self.serial += 1
        path = Path(self.directory.name)/f"{self.serial:06d}.zmx"
        self.ctx.backend.save_as(path)
        if self.loaded is not None:
            self.unclosed[path] = getattr(self.loaded, "rtc_unclosed", False)
        return path

    def restore(self, path):
        self.ctx.backend.load(path)
        self.ctx.cache.clear()
        if self.loaded is not None and path in self.unclosed and hasattr(self.loaded, "mark_rtc"):
            self.loaded.mark_rtc(self.unclosed[path])

    def commit(self, path):
        self.restore(path)
        self.rollback = False
        self.changed = True

    def report_progress(self, stage: str, **data) -> None:
        """每试完一个候选写进度并（节流地）刷新流程图窗口；worker 上下文没有 progress，什么也不做。
        Report progress after each candidate and refresh the workflow window now and then; a no-op without ctx.progress."""
        if hasattr(self.ctx, "progress"):
            self.ctx.progress(stage, **data)

    def __exit__(self, kind, value, trace):
        try:
            if kind is not None or self.rollback:
                self.restore(self.entry)
                if self.loaded is not None and self.was_dirty is not None:
                    self.loaded.dirty = self.was_dirty
            elif self.changed:
                self.ctx.mark_dirty()
        finally:
            self.ctx._expert_active = False
            self.directory.cleanup()
