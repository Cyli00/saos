"""优化试探点的可微定义域惩罚与接受步关卡；光线和面形仍由 Optiland 计算。"""

from __future__ import annotations

import copy
import inspect
import math
from contextlib import contextmanager

import numpy as np

from saos import timing
from saos.protocol import ErrorCode, SaosError

ROOT_FLOOR = 1e-12
MIN_VALID_FRACTION = 0.95
MAX_BACKTRACKS = 12


class InvalidTrial(SaosError):
    """试点超出可微定义域；初值仍报错，优化中的试点可以拒绝并重试。"""

    def __init__(self, message):
        super().__init__(ErrorCode.OPTILAND_ERROR, message)


@contextmanager
def aiming_without_records(optic):
    """瞄准只需要末端光线，禁止内部试探追迹覆盖各面的分析记录。"""
    restores = []
    try:
        for surface in optic.surfaces:
            previous = vars(surface).get("trace")
            original = surface.trace
            def trace(rays, record=False, original=original):
                return original(rays, record=False)
            restores.append((surface, previous))
            surface.trace = trace
        yield
    finally:
        for surface, previous in restores:
            if previous is None:
                del surface.trace
            else:
                surface.trace = previous


class DomainPenalty:
    def __init__(self, backend):
        self.backend = backend
        self.terms = []
        self.restores = []

    def add(self, margin):
        import torch

        if isinstance(margin, torch.Tensor) and torch.is_grad_enabled():
            if not bool(torch.isfinite(margin).all()):
                raise InvalidTrial("the differentiable ray-domain penalty is non-finite; reject this trial")
            if margin.numel():
                self.terms.append(torch.relu(margin).square().mean())

    def replace(self, obj, name, replacement):
        own = name in vars(obj)
        self.restores.append((obj, name, own, vars(obj).get(name)))
        setattr(obj, name, replacement)

    def aperture(self, rays, spec, aperture):
        import torch

        x = rays.x - float(spec.get("x_decenter", 0.0))
        y = rays.y - float(spec.get("y_decenter", 0.0))
        kind = spec.get("type", "circular")
        if kind.startswith("rectangular"):
            hx, hy = float(spec["x_half"]), float(spec["y_half"])
            outside = torch.maximum(x.square() / hx**2 - 1, y.square() / hy**2 - 1)
        elif kind.startswith("elliptical"):
            outside = x.square() / float(spec["x_half"])**2 + y.square() / float(spec["y_half"])**2 - 1
        else:
            outer = float(spec.get("max", getattr(aperture, "r_max", math.inf)))
            inner = float(spec.get("min", getattr(aperture, "r_min", 0.0)))
            radial = x.square() + y.square()
            outside = radial / max(outer**2, ROOT_FLOOR) - 1
            if inner > 0:
                outside = torch.maximum(outside, 1 - radial / inner**2)
        self.add(-outside if kind.endswith("obscuration") else outside)

    @contextmanager
    def guarded(self):
        import torch
        import optiland.backend as be

        sqrt, conic = be.sqrt, be.conic_intersection

        def safe_sqrt(value):
            if not isinstance(value, torch.Tensor):
                return sqrt(value)
            self.add(-value)
            return sqrt(torch.clamp(value, min=ROOT_FLOOR))

        def safe_conic(x, y, z, l, m, n, radius, k, **kwargs):
            # 使用原生求交的同一二次方程，只取定义域距离作损失，不用它计算交点。
            curvature = 1 / radius
            a = curvature * (l*l + m*m + (1 + k)*n*n)
            b = 2 * (curvature * (x*l + y*m + (1 + k)*z*n) - n)
            c = curvature * (x*x + y*y + (1 + k)*z*z) - 2*z
            cross = 4*a*c
            self.add((cross - b*b) / (1 + b*b + torch.sqrt(1 + cross*cross)))
            distance = conic(x, y, z, l, m, n, radius, k, **kwargs)
            # 无交点的分支只用于惩罚续算；原始追迹另外决定哪些光线有物理意义。
            return torch.where(torch.isfinite(distance), distance, torch.zeros_like(distance))

        try:
            self.replace(be, "sqrt", safe_sqrt)
            self.replace(be, "conic_intersection", safe_conic)
            seen = set()
            for i, surface in enumerate(self.backend.optic.surfaces):
                self.replace(surface, "_distance_capability", None)
                geometry = surface.geometry
                if id(geometry) not in seen:
                    seen.add(id(geometry))
                    original = geometry.distance
                    accepts_aperture = "aperture" in inspect.signature(original).parameters
                    residual = getattr(geometry, "_surface_residual", None)
                    length = max(float(self.backend._rows[i].get("semi_diameter") or 1), 1e-6)

                    def distance(rays, aperture=None, original=original, accepts=accepts_aperture, residual=residual, length=length):
                        value = original(rays, aperture=aperture) if accepts else original(rays)
                        value = torch.where(torch.isfinite(value), value, torch.zeros_like(value))
                        if residual is not None:
                            # 非球面求交失败时，原生残差保留对面参数和入射光线的导数。
                            error = residual(value.detach(), rays) / length
                            self.add(error)
                            self.add(-error)
                        return value

                    self.replace(geometry, "distance", distance)
                ap = surface.aperture
                if ap is not None and id(ap) not in seen:
                    seen.add(id(ap))
                    original_clip, spec = ap.clip, self.backend._rows[i].get("aperture") or {}

                    def clip(rays, original=original_clip, spec=spec, ap=ap):
                        self.aperture(rays, spec, ap)
                        return original(rays)

                    self.replace(ap, "clip", clip)
            yield self
        finally:
            for obj, name, own, previous in reversed(self.restores):
                if own:
                    setattr(obj, name, previous)
                else:
                    delattr(obj, name)
            self.restores.clear()

    def trace(self, wave, hx, hy, px, py, target):
        import torch
        import optiland.backend as be
        from saos.backend.optiland_torch_aiming import differentiable_aiming

        nhx, nhy = self.backend._native_fields(hx, hy)
        um = self.backend._effective["wavelengths"]["um"][wave - 1]
        with self.guarded(), differentiable_aiming(self.backend), be.grad_mode.temporary_enable():
            rays = self.backend.optic.ray_tracer.ray_generator.generate_rays(float(nhx), float(nhy), be.array(px), be.array(py), um)
            for surface in self.backend.optic.surfaces[:target + 1]:
                surface.trace(rays, record=False)
        return torch.stack(self.terms).sum() if self.terms else torch.zeros((), dtype=torch.float64, device=be.get_device())


class StepGuard:
    """在独立的进程内 Optic 上同步候选步，不切断主优化器的计算图。"""

    def __init__(self, problem):
        from saos.backend.optiland import OptilandBackend
        from saos.backend.optiland_materials import MaterialStore

        self.problem = problem
        self.probe = OptilandBackend()
        self.probe.device = "cpu"
        for name in ("_merit", "_merit_script", "_bounds", "_glass_vary"):
            setattr(self.probe, name, copy.deepcopy(getattr(problem.b, name)))
        self.prescription = copy.deepcopy(problem.b._require())
        self.materials = MaterialStore(problem.b._store.records)
        self.config = problem.b.current_configuration()
        self.aiming = problem.b._aiming
        self.last_reason = None
        self.backtracks = 0

    @timing.timed("guard.admissible")
    def admissible(self, x):
        from saos.backend.optiland_operands import _gq_pupil

        if not np.isfinite(x).all() or np.any(x < self.problem.lo) or np.any(x > self.problem.hi):
            self.last_reason = "variable_bounds"
            return False
        b = self.probe
        try:
            if b._prescription is None:
                b._store = self.materials
                b.build(copy.deepcopy(self.prescription))
                if self.config != 1:
                    b.select_configuration(self.config)
                b.set_aiming(self.aiming)
            b._commit(self.problem.physical(x))
            clash = b.interference()
            if clash.get("crossings") or clash.get("overlaps"):
                self.last_reason = "parts_interference"
                return False
            px, py, _ = _gq_pupil(3)
            px, py = np.append(px, 0), np.append(py, 0)
            for hx, hy in b._normalized_fields():
                for wave in range(1, b.wavelength_count() + 1):
                    rays = b.trace_batch([(wave, hx, hy, x, y) for x, y in zip(px, py)], len(b._rows) - 1)
                    if float(rays.ok.mean()) < MIN_VALID_FRACTION:
                        self.last_reason = "insufficient_valid_rays"
                        return False
            self.last_reason = None
            return True
        except (SaosError, ValueError, FloatingPointError, ZeroDivisionError) as exc:
            self.last_reason = f"candidate_rebuild: {exc}"
            return False
        finally:
            self.problem.b._configure()

    def close(self):
        self.probe.close()
        self.problem.b._configure()

    @timing.timed("guard.accept_apertures")
    def accept_apertures(self):
        for i, row in enumerate(self.probe._rows):
            if row.get("_auto_semi_diameter"):
                self.problem.b._rows[i]["semi_diameter"] = row["semi_diameter"]
                self.problem.b._rows[i]["_auto_semi_diameter"] = True
                self.problem.b.optic.surfaces[i].semi_aperture = row["semi_diameter"]
