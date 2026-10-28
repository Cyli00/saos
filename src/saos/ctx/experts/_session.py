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
    if info.get("backend") == "rtc":
        raise SaosError(ErrorCode.BACKEND_UNSUPPORTED, "experts require the authoritative ZOS lens, not an rtc exploration copy")
    if info.get("mode") == "non_sequential" or info.get("nsc_groups"):
        raise SaosError(ErrorCode.BACKEND_UNSUPPORTED, "experts currently require a purely sequential lens")
    if ctx.backend.configuration_count() != 1:
        raise SaosError(ErrorCode.BACKEND_UNSUPPORTED,
                        "experts currently require one configuration; multi-configuration pickups must not be guessed")
    return info


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
