"""Optiland 阶段的 torch 自动微分优化（torch-trf、torch-lbfgs、torch-adam，Plan 2 §2.2、§2.3）。

只对可微子集求梯度：RSCE、RWCE、REAY、EFFL、TOTR、CTVA、ETVA 以及由 CTVA / ETVA 构成的边界行（CTGT、CTLT、ETGT、ETLT），
再加上返回 Tensor 的损失脚本（在脚本里用 ctx.backend.torch_operand 取可微的操作数值）。评价函数里有别的操作数时直接报错并列出，
不静默退回有限差分。变量坐标与 scipy 路径相同（optiland_opt.Problem 的缩放）。

原始追迹决定光线有效性；另一次受保护的原生追迹给出全反射、求交定义域、非球面求交残差和孔径越界的连续惩罚。
受保护路径的交点不作为物理结果。每个优化步都按有效光线与交叠关卡接受或逐次减半，只有已接受步能成为最终结果。
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from saos.backend.base import is_par
from saos.backend.optiland_guard import DomainPenalty, InvalidTrial, MIN_VALID_FRACTION, StepGuard
from saos.backend.optiland_opt import BOUNDARY, CYCLES, PENALTY, REPORT_EVERY_S, Problem, evaluate, merit
from saos.backend.optiland_budget import Snapshot, outcome
from saos.backend.optiland_state import array
from saos.backend.optiland_torch_aiming import differentiable_aiming
from saos.protocol import ErrorCode, SaosError

TORCH_OPERANDS = {"RSCE", "RWCE", "REAY", "EFFL", "TOTR", "CTVA", "ETVA", *BOUNDARY}


class TorchUnavailable(SaosError):
    """明确的可微能力缺口；只有自动选择算法时允许据此回退。"""

    def __init__(self, reason: str, message: str):
        super().__init__(ErrorCode.BAD_ARGS, message, {"kind": "torch_unavailable", "reason": reason})
        self.reason = reason


# 曲率恰为 0（平面）时 1/c 的梯度是无穷大；取一个不影响矢高的极小曲率
FLAT = 1e-12
TORCH_MAX_BACKTRACKS = 32


class TorchState:
    """一次 torch 运行里的可微量：变量（缩放坐标的 Parameter 换算成的物理值）、受其影响的厚度与求解结果、各面的基准位置。"""

    def __init__(self, b, problem: Problem):
        self.b, self.problem = b, problem
        self.base = [tuple(float(np.asarray(array(v)).ravel()[0]) for v in (s.geometry.cs.x, s.geometry.cs.y, s.geometry.cs.z))
                     for s in b.optic.surfaces]
        self.thickness0 = {i: float(b._rows[i].get("thickness", 0.0)) for i in range(len(b._rows))}
        self.values: dict[tuple[int, str], Any] = {}
        self.domain_keys = set()
        self.penalties = []

    def penalize(self, wave, hx, hy, px, py, target):
        key = (wave, hx, hy, px.tobytes(), py.tobytes(), target)
        if key not in self.domain_keys:
            self.domain_keys.add(key)
            self.penalties.append(DomainPenalty(self.b).trace(wave, hx, hy, px, py, target))

    def write(self, i: int, column: str, value) -> None:
        """在 torch Optic 上写入一个有效值（可带梯度）；厚度只记下，位置在 place() 里一次算完。"""
        b = self.b
        self.values[(i, column)] = value
        surface = b.optic.surfaces[i]
        if column == "radius":
            b.optic.updater.set_radius(value, i)
        elif column == "conic":
            surface.geometry.k = value
        elif is_par(column):
            surface.geometry.coefficients[int(column[3:]) - 1] = value
        elif column != "thickness":
            raise SaosError(ErrorCode.BAD_ARGS, f"{column} cannot vary on the torch path")

    def thickness(self, i: int):
        return self.values.get((i, "thickness"), self.thickness0[i])

    def place(self) -> None:
        """各面位置 = 基准位置 + Σ（其前各面厚度变化 × 该面厚度方向），可对厚度求导。"""
        import torch

        b = self.b
        changed = sorted(i for (i, column) in self.values if column == "thickness")
        if not changed:
            return
        for i in changed:
            if i == 0:
                b.optic.surfaces[0].geometry.cs.z = -self.values[(0, "thickness")]
        for j, s in enumerate(b.optic.surfaces):
            if j == 0:
                continue
            shift = None
            for i in changed:
                if 0 < i < j:
                    step = (self.values[(i, "thickness")] - self.thickness0[i]) * torch.as_tensor(b.optic.saos_axes[i], dtype=torch.float64,
                                                                                                device=_device())
                    shift = step if shift is None else shift + step
            if shift is not None:
                x, y, z = self.base[j]
                s.geometry.cs.x, s.geometry.cs.y, s.geometry.cs.z = x + shift[0], y + shift[1], z + shift[2]

    def settle(self) -> None:
        """拾取、F 数（Optiland 的求解器在 torch 后端上可微）和零区域边缘光线高度（闭式）的可微版本。"""
        b = self.b
        done, visiting = set(), set()

        def settle(i, column):
            node = (i, column)
            if node in done:
                return
            if node in visiting:
                raise SaosError(ErrorCode.BAD_ARGS, f"cyclic solve dependency at {i}.{column}")
            visiting.add(node)
            spec = b._rows[i].get("solves", {}).get(column, {})
            kind_ = spec.get("type")
            if kind_ == "pickup" and column != "material":
                settle(spec["surface"], column)
                source = self.values.get((spec["surface"], column))
                if source is None:
                    source = self.thickness0[spec["surface"]] if column == "thickness" else b.surface_value(spec["surface"], column)
                scale = spec.get("scale", 1.0)
                self.write(i, column, (0.0 if scale == 0 else source * scale) + spec.get("offset", 0.0))
            elif kind_ in ("f_number", "marginal_ray_height"):
                for j in range(i):
                    for key in b._rows[j].get("solves", {}):
                        settle(j, key)
                self.place()
                if kind_ == "f_number":
                    from optiland.solves.curvature import MarginalRayAngleCurvatureSolve

                    MarginalRayAngleCurvatureSolve(b.optic, i, -1 / (2 * spec["value"])).apply()
                    self.values[(i, "radius")] = b.optic.surfaces[i].geometry.radius
                else:
                    y, u = b.optic.paraxial.marginal_ray()
                    y, u = y.reshape(-1), u.reshape(-1)
                    self.write(i, "thickness", self.thickness(i) + (spec["height"] - y[i + 1]) / u[i])
            visiting.remove(node)
            done.add(node)

        for i, row in enumerate(b._rows):
            for column in row.get("solves", {}):
                settle(i, column)
        self.place()


def _clean(param) -> None:
    """梯度为空时补零；非有限导数明确报错，不能伪装成平坦方向。"""
    import torch

    if param.grad is None:
        param.grad = torch.zeros_like(param)
    elif not torch.isfinite(param.grad).all():
        raise InvalidTrial("the merit gradient is non-finite")


def _device() -> str:
    import optiland.backend as be

    return str(be.get_device()) if hasattr(be, "get_device") else "cpu"


def _check(b, rows: list[dict[str, Any]]) -> None:
    others = sorted({row["type"] for row in rows} - TORCH_OPERANDS)
    if others:
        raise TorchUnavailable("operands", f"the torch algorithms differentiate only {sorted(TORCH_OPERANDS)}; the merit function also has "
                                            f"{others}. Use --algorithm trf (finite differences over every mapped operand)")
    for i, row in enumerate(b._rows):
        for column, spec in row.get("solves", {}).items():
            if spec.get("type") == "marginal_ray_height" and spec.get("zone", 0):
                raise TorchUnavailable("solve", f"surface {i}: a marginal-ray-height solve with a nonzero zone is not differentiable "
                                                    "here; use --algorithm trf")


def _rays(b, wave: int, hx: float, hy: float, px: np.ndarray, py: np.ndarray, target: int):
    """带梯度地追迹到 target 面，返回 (rays, 有效掩码)；无效光线先不带梯度找出、再从这一组里去掉，NaN 不进计算图。"""
    import torch

    import optiland.backend as be

    nhx, nhy = b._native_fields(hx, hy)
    um = b._effective["wavelengths"]["um"][wave - 1]
    b._torch_state.penalize(wave, hx, hy, px, py, target)

    def trace(x, y, grad: bool):
        with differentiable_aiming(b), be.grad_mode.temporary_enable() if grad else torch.no_grad():
            rays = b.optic.ray_tracer.ray_generator.generate_rays(float(nhx), float(nhy), be.array(x), be.array(y), um)
            for i, surface in enumerate(b.optic.surfaces):
                if i > target:
                    break
                surface.trace(rays, record=False)
            b.optic.surfaces[target].geometry.cs.localize(rays)
        return rays

    try:
        probe = trace(px, py, grad=False)
    except (ValueError, ZeroDivisionError, FloatingPointError, RuntimeError):
        return None, np.zeros(len(px), dtype=bool)
    values = np.column_stack([np.asarray(array(getattr(probe, k)), dtype=float).ravel() for k in ("x", "y", "z", "L", "M", "N")])
    valid = np.isfinite(values).all(axis=1) & (np.asarray(array(probe.i), dtype=float).ravel() > 0)
    if not valid.any():
        return None, valid
    try:
        return trace(px[valid], py[valid], grad=True), valid
    except (ValueError, ZeroDivisionError, FloatingPointError, RuntimeError):
        return None, np.zeros(len(px), dtype=bool)


def operand(b, state: TorchState, name: str, int1: int, int2: int, data: list[float], target: float = 0.0):
    """可微操作数的 torch 值，返回 (Tensor 或 float, 失败光线比例)。参数位置同 optiland_operands.operand。"""
    import torch

    from saos.backend.optiland_operands import _gq_pupil, _surface, _wave

    name = name.upper()
    if name in BOUNDARY:
        base, sign = BOUNDARY[name]
        raw, bad = operand(b, state, base, int1, 0, [0.0, 0.0] if base == "ETVA" else [])
        raw = torch.as_tensor(raw, dtype=torch.float64, device=_device())
        return torch.where(sign * (raw - target) >= 0, torch.as_tensor(target, dtype=torch.float64, device=_device()), raw), bad
    if name == "TOTR":
        return sum(state.thickness(i) for i in range(1, len(b._rows) - 1)), 0.0
    if name == "CTVA":
        return state.thickness(_surface(b, int1)), 0.0
    if name == "ETVA":
        zone, mode = (list(data) + [0.0, 0.0])[:2]
        surface = _surface(b, int1)
        if not 1 <= surface < len(b._rows) - 1:
            raise SaosError(ErrorCode.BAD_ARGS, "ETVA requires a surface with a successor")
        if zone or int2 or int(mode) not in (0, 1):
            raise TorchUnavailable("operand_parameters", "ETVA on the torch path takes Code 0, zone 0 and Mode 0 or 1 on a surface with a successor")
        r = float(b.surface_value(surface, "semi_diameter" if int(mode) else "mech_semi_diameter"))
        zero, edge = torch.zeros(1, dtype=torch.float64, device=_device()), torch.full((1,), r, dtype=torch.float64, device=_device())
        sag = lambda k: b.optic.surfaces[k].geometry.sag(zero, edge).reshape(-1)[0]
        return state.thickness(surface) + sag(surface + 1) - sag(surface), 0.0
    if name == "EFFL":
        wave = _wave(b, int2)
        before = b.optic.wavelengths.primary_index
        try:
            b.optic.wavelengths.primary_index = wave - 1
            return b.optic.paraxial.f2().reshape(-1)[0], 0.0
        finally:
            b.optic.wavelengths.primary_index = before
    if name == "REAY":
        hx, hy, px, py = [float(v) for v in (list(data) + [0.0] * 4)[:4]]
        rays, valid = _rays(b, _wave(b, int2), hx, hy, np.array([px]), np.array([py]), _surface(b, int1))
        if rays is None:
            return math.nan, 1.0
        return rays.y.reshape(-1)[0], 0.0
    if name in ("RSCE", "RWCE"):
        hx, hy = [float(v) for v in (list(data) + [0.0, 0.0])[:2]]
        px, py, weights = _gq_pupil(int1)
        waves = [_wave(b, int2)] if int2 else list(range(1, b.wavelength_count() + 1))
        wave_weights = b._effective["wavelengths"]["weights"]
        if name == "RSCE":
            xs, ys, ws, bad = [], [], [], 0.0
            for w in waves:
                rays, valid = _rays(b, w, hx, hy, px, py, len(b._rows) - 1)
                bad += float((~valid).mean()) / len(waves)
                if rays is None:
                    continue
                xs.append(rays.x.reshape(-1)), ys.append(rays.y.reshape(-1))
                ws.append(torch.as_tensor(weights[valid] * (wave_weights[w - 1] if not int2 else 1.0), dtype=torch.float64, device=_device()))
            if not xs:
                return math.nan, 1.0
            x, y, wt = torch.cat(xs), torch.cat(ys), torch.cat(ws)
            wt = wt / wt.sum()
            cx, cy = (wt * x).sum(), (wt * y).sum()
            return torch.sqrt((wt * ((x - cx) ** 2 + (y - cy) ** 2)).sum() + 1e-30), bad
        return _rwce(b, waves, int2, hx, hy, px, py, weights, wave_weights)
    raise TorchUnavailable("operands", f"{name} has no torch implementation")


def _rwce(b, waves, int2, hx, hy, px, py, weights, wave_weights):
    """RWCE：去掉加权活塞与倾斜后的 RMS 波前（波），按 Wavefront 的主光线参考球面；无效光线先剔除。"""
    import torch

    from optiland.distribution import create_distribution
    from optiland.wavefront.strategy import ChiefRayStrategy
    from optiland.wavefront.reference_geometry import SphericalReference

    import optiland.backend as be

    class DifferentiableChiefRay(ChiefRayStrategy):
        def _create_spherical_ref(self, x, y, z):
            # 原策略的 item() 会切断主光线与出瞳的导数；其余追迹、光程和参考球求交全部复用。
            xp_x, xp_y, xp_z = self.exit_pupil_point
            radius = torch.sqrt((x - xp_x) ** 2 + (y - xp_y) ** 2 + (z - xp_z) ** 2)
            return SphericalReference((x.reshape(()), y.reshape(()), z.reshape(())), radius.reshape(()))

    nhx, nhy = b._native_fields(hx, hy)
    total, norm, bad = 0.0, 0.0, 0.0
    for w in waves:
        um = b._effective["wavelengths"]["um"][w - 1]
        b._torch_state.penalize(w, hx, hy, px, py, len(b._rows) - 1)

        def opd(x, y, grad: bool):
            distribution = create_distribution("uniform")
            distribution.x, distribution.y = be.array(x), be.array(y)
            with differentiable_aiming(b), be.grad_mode.temporary_enable() if grad else torch.no_grad():
                data = DifferentiableChiefRay(b.optic, distribution).compute_wavefront_data((float(nhx), float(nhy)), um)
            return data.opd.reshape(-1), data.intensity.reshape(-1)

        try:
            probe, intensity = opd(px, py, grad=False)
        except (ValueError, ZeroDivisionError, FloatingPointError, RuntimeError):
            bad += 1.0 / len(waves)
            continue
        valid = np.isfinite(np.asarray(array(probe), dtype=float)) & (np.asarray(array(intensity), dtype=float) > 0)
        bad += float((~valid).mean()) / len(waves)
        if not valid.any():
            continue
        values, _ = opd(px[valid], py[valid], grad=True)
        if valid.sum() < 3:
            bad += float(valid.mean()) / len(waves)
            continue
        wt = torch.as_tensor(weights[valid], dtype=torch.float64, device=_device())
        design = torch.as_tensor(np.column_stack([np.ones(valid.sum()), px[valid], py[valid]]), dtype=torch.float64, device=_device())
        normal = design.T @ (design * wt[:, None])
        coef = torch.linalg.solve(normal, design.T @ (wt * values))
        residual = values - design @ coef
        weight = wave_weights[w - 1] if not int2 else 1.0
        total = total + weight * (wt * residual ** 2).sum() / wt.sum()
        norm += weight
    if norm <= 0:
        return math.nan, 1.0
    return torch.sqrt(total / norm + 1e-30), bad


def optimize(b, *, algorithm: str, cycles: str, max_seconds: float | None, on_progress, should_cancel,
             script: Callable[[], Any] | None, script_weight: float, device: str | None) -> dict[str, Any]:
    import torch

    if not hasattr(b, "_opt_budget"):
        from saos.backend.optiland_opt import optimize as run
        return run(b, algorithm=algorithm, cycles=cycles, max_seconds=max_seconds, on_progress=on_progress,
                   should_cancel=should_cancel, script=script, script_weight=script_weight, device=device)
    if cycles not in CYCLES:
        raise SaosError(ErrorCode.BAD_ARGS, f"cycles must be one of {list(CYCLES)}")
    if device == "cuda" and not torch.cuda.is_available():
        raise SaosError(ErrorCode.BAD_ARGS, "--device cuda: torch sees no CUDA device")
    rows = list(b._merit)
    if not rows and script is None:
        raise SaosError(ErrorCode.BAD_ARGS, "the merit function is empty; use saos opt merit wizard, add or script first")
    _check(b, rows)
    b._sync_optical_state()
    problem = Problem(b, rows, script, script_weight)
    if not problem.live:
        raise TorchUnavailable("variables", "the torch path sets radius, curvature, thickness, conic and even/odd asphere terms in place; "
                                            "this lens has another kind of variable or pickup (or a varying model glass). Use trf")
    budget = b._opt_budget
    initial = None
    weights = [row["weight"] for row in rows] + ([script_weight] if script is not None else [])
    total = sum(weights) or 1.0
    scale = [math.sqrt(w / total) for w in weights]
    chosen_device = "cuda" if device == "cuda" else "cpu"
    unit = torch.as_tensor(problem.unit, dtype=torch.float64, device=chosen_device)
    lo = torch.as_tensor(problem.lo, dtype=torch.float64, device=chosen_device)
    hi = torch.as_tensor(problem.hi, dtype=torch.float64, device=chosen_device)
    started = time.monotonic()
    out_state: dict[str, Any] = {"evaluations": 0, "stopped_by": None, "best": (math.inf, problem.x0.copy()), "reported": started,
                                 "bad": 0.0, "accepted_steps": 0, "rejected_steps": 0}
    guard = StepGuard(problem)
    message = ""
    converged = False
    b._torch_device = "cuda" if device == "cuda" else "cpu"
    try:
        b._synchronized = None
        b._sync_optical_state()
        state = TorchState(b, problem)
        b._torch_state = state
        param = torch.nn.Parameter(torch.as_tensor(problem.x0, dtype=torch.float64, device=chosen_device))

        def loss_of(x, *, vector=False):
            budget.check()
            state.values.clear()
            state.domain_keys.clear()
            state.penalties.clear()
            bounded = torch.maximum(torch.minimum(x, hi), lo)
            physical = bounded / unit
            for k, (i, column) in enumerate(problem.keys):
                v = physical[k]
                if column == "radius":
                    floor = torch.where(v < 0, torch.full_like(v, -FLAT), torch.full_like(v, FLAT))
                    bounded_curvature = torch.where(v.abs() < FLAT, floor, v)
                    # 平面只正则化正向半径，反向仍以曲率为自变量，避免平面变量的梯度被夹成零。
                    c = v + (bounded_curvature - v).detach()
                    v = 1.0 / c
                v = v * b._scales.get((i, column), 1.0)
                state.write(i, column, v)
            state.settle()
            terms, bad = [], 0.0
            out_state["script_value"] = None
            for k, row in enumerate(rows):
                value, failed = operand(b, state, row["type"], row["int1"], row["int2"], row["data"], row["target"])
                bad += failed
                # 与变量无关的行是常数（float），照常计入
                value = value if isinstance(value, torch.Tensor) else torch.as_tensor(float(value), dtype=torch.float64, device=chosen_device)
                if not torch.isfinite(value):
                    terms.append(torch.as_tensor(scale[k] * PENALTY, dtype=torch.float64, device=chosen_device))
                else:
                    terms.append(scale[k] * (value - row["target"]))
            if script is not None:
                try:
                    value = script()
                except SaosError:
                    raise
                except Exception as exc:
                    raise SaosError(ErrorCode.SCRIPT_ERROR, f"merit script raised {type(exc).__name__}: {exc}") from exc
                if not isinstance(value, torch.Tensor):
                    try:
                        scalar = float(value)
                    except (TypeError, ValueError) as exc:
                        raise SaosError(ErrorCode.SCRIPT_ERROR, "the merit loss script must return one number") from exc
                    if not math.isfinite(scalar):
                        raise SaosError(ErrorCode.SCRIPT_ERROR, "the merit loss script returned a non-finite value")
                    raise TorchUnavailable("script_not_tensor", "the merit script returned a float; only a Tensor (from ctx.backend.torch_operand or "
                                                        "ctx.backend.optic) works with the torch algorithms. Use trf")
                if value.numel() != 1:
                    raise SaosError(ErrorCode.SCRIPT_ERROR, "the merit loss script must return a scalar Tensor")
                if not torch.isfinite(value).all():
                    raise SaosError(ErrorCode.SCRIPT_ERROR, "the merit loss script returned a non-finite Tensor")
                if not value.requires_grad or torch.autograd.grad(value, x, retain_graph=True, allow_unused=True)[0] is None:
                    raise TorchUnavailable("script_detached", "the merit loss Tensor is not connected to the optimisation variables")
                value = value.to(device=chosen_device, dtype=torch.float64)
                out_state["script_value"] = float(value.detach())
                terms.append(scale[-1] * value.reshape(()) if torch.isfinite(value).all()
                             else torch.as_tensor(scale[-1] * PENALTY, dtype=torch.float64, device=chosen_device))
            r = torch.stack([t.reshape(()) for t in terms])
            if not state.domain_keys:
                from saos.backend.optiland_operands import _gq_pupil

                px, py, _ = _gq_pupil(3)
                for hx, hy in b._normalized_fields():
                    for wave in range(1, b.wavelength_count() + 1):
                        state.penalize(wave, hx, hy, px, py, len(b._rows) - 1)
            domain = torch.stack(state.penalties).sum() if state.penalties else x.sum() * 0
            # 边界由 clamp 保证、越界方向梯度为零；不再加二次罚项，否则线搜索第一步的越界试点会被压成远大于 merit 的值而原地不动。
            out_state["current_merit"] = float(torch.linalg.vector_norm(r).detach())
            r = torch.cat((r, (PENALTY * torch.sqrt(domain + 1e-30)).reshape(1))) + 0.0 * x.sum()
            loss = (r * r).sum()
            if not torch.isfinite(loss):
                raise InvalidTrial("the differentiable merit or domain penalty is non-finite")
            out_state["evaluations"] += 1
            budget.result["evaluations"] = out_state["evaluations"]
            out_state["bad"] = bad
            now = time.monotonic()
            if on_progress is not None and now - out_state["reported"] >= REPORT_EVERY_S:
                out_state["reported"] = now
                best = out_state["best"][0]
                on_progress({"merit": best if math.isfinite(best) else None,
                             "evaluations": out_state["evaluations"], "elapsed_s": round(now - started, 2)})
            return r if vector else loss

        def remember(point, value):
            value = out_state.get("verified_merit", value)
            previous = out_state["best"][0]
            if not math.isfinite(previous) or value < previous - 1e-12 * max(1., abs(previous)):
                snapshot = Snapshot(guard.probe)
                if math.isfinite(previous) and not np.array_equal(point, out_state["best"][1]):
                    out_state["accepted_steps"] += 1
                    budget.result["accepted_steps"] = out_state["accepted_steps"]
                out_state["best"] = (value, point.copy())
                budget.best = snapshot
                budget.result["final_merit"] = value
                return True
            return False

        def acceptable(point, value):
            if not (math.isfinite(value) and out_state["bad"] == 0 and guard.admissible(point)):
                return False
            try:
                values, errors = evaluate(guard.probe, rows)
                verified = merit(rows, values, out_state["script_value"], script_weight)
            finally:
                b._configure()
            if any(errors) or not math.isfinite(verified):
                return False
            if abs(verified - value) > 1e-8 * max(1., abs(value)):
                raise SaosError(ErrorCode.OPTILAND_ERROR, "Torch merit disagrees with the rebuilt lens",
                                {"torch_merit": value, "rebuilt_merit": verified})
            out_state["verified_merit"] = verified
            return True

        def accept_step(before, proposal, optimizer):
            base_value = out_state["best"][0]
            for attempt in range(TORCH_MAX_BACKTRACKS + 1):
                budget.check()
                if attempt:
                    guard.backtracks += 1
                with torch.no_grad():
                    trial = before + (proposal - before) * 2.0**(-attempt)
                    param.copy_(torch.maximum(torch.minimum(trial, hi), lo))
                if torch.equal(before, param):
                    break
                try:
                    loss_of(param)
                except InvalidTrial:
                    out_state["rejected_steps"] += 1
                    budget.result["rejected_steps"] = out_state["rejected_steps"]
                    continue
                value = out_state["current_merit"]
                point = param.detach().cpu().numpy().copy()
                if value < base_value - 1e-12 * max(1., abs(base_value)) and acceptable(point, value):
                    if remember(point, value):
                        if attempt:
                            optimizer.state.clear()
                        return out_state["best"][0]
            with torch.no_grad():
                param.copy_(before)
            optimizer.state.clear()
            out_state["rejected_steps"] += 1
            budget.result["rejected_steps"] = out_state["rejected_steps"]
            return None

        limit = CYCLES[cycles]
        import optiland.backend as be

        with be.grad_mode.temporary_enable():
            loss_of(param)
            initial = out_state["current_merit"]
            budget.result["initial_merit"] = initial
            budget.result["final_merit"] = initial
            budget.result.update(variables=len(problem.keys), targets=len(rows) + (script is not None),
                                 device=chosen_device, inner_loop="torch")
            if not math.isfinite(initial):
                raise SaosError(ErrorCode.OPTILAND_ERROR, "initial merit is non-finite")
            if not acceptable(problem.x0, initial):
                raise SaosError(ErrorCode.OPTILAND_ERROR, "the initial lens fails the optical step guard")
            remember(problem.x0, initial)
            if algorithm == "torch-trf":
                from scipy.optimize import least_squares
                budget.watch(least_squares)

                cache = {}
                def residuals(point):
                    budget.check()
                    leaf = torch.tensor(point, dtype=torch.float64, device=chosen_device, requires_grad=True)
                    try:
                        r = loss_of(leaf, vector=True)
                        jac = torch.stack([torch.autograd.grad(component, leaf, retain_graph=True)[0] for component in r])
                        if not torch.isfinite(r).all() or not torch.isfinite(jac).all():
                            raise InvalidTrial("non-finite autograd residual or Jacobian")
                    except InvalidTrial:
                        if not cache:
                            raise
                        # SciPy TRF 对非有限试点缩小信赖域并重试，不请求该点的雅可比。
                        out_state["rejected_steps"] += 1
                        budget.result["rejected_steps"] = out_state["rejected_steps"]
                        return np.full(len(weights) + 1, np.inf)
                    value = out_state["current_merit"]
                    values = r.detach().cpu().numpy().copy()
                    derivative = jac.detach().cpu().numpy().copy()
                    if value < out_state["best"][0] and acceptable(point, value):
                        remember(point, value)
                    elif value < out_state["best"][0]:
                        # 拒绝的坐标必须返回自身的罚值，不能用回退点冒充原坐标的残差。
                        values = np.full_like(values, np.inf)
                        derivative = np.zeros_like(derivative)
                        out_state["rejected_steps"] += 1
                        budget.result["rejected_steps"] = out_state["rejected_steps"]
                    cache.update(point=point.copy(), jac=derivative)
                    return values

                def jacobian(point):
                    if "point" not in cache or not np.array_equal(point, cache["point"]):
                        residuals(point)
                    return cache["jac"]

                result = least_squares(residuals, problem.x0, jac=jacobian, bounds=(problem.lo, problem.hi),
                                       method="trf", x_scale="jac", max_nfev=None if limit is None else limit + 1)
                converged = bool(result.success and np.allclose(result.x, out_state["best"][1], rtol=1e-7, atol=1e-9))
                message = str(result.message)
            else:
                if algorithm == "torch-lbfgs":
                    optimizer = torch.optim.LBFGS([param], lr=1.0, max_iter=1, history_size=20,
                                                 line_search_fn="strong_wolfe", tolerance_grad=1e-12, tolerance_change=1e-15)
                else:
                    optimizer = torch.optim.Adam([param], lr=1e-2)
                for _ in range((limit or 100) * (50 if algorithm == "torch-adam" else 1)):
                    budget.check()
                    before = param.detach().clone()
                    def closure():
                        optimizer.zero_grad()
                        loss = loss_of(param)
                        loss.backward()
                        _clean(param)
                        return loss
                    closure()
                    gradient = param.grad.detach().clone()
                    projected = torch.where(((param <= lo) & (gradient > 0)) | ((param >= hi) & (gradient < 0)), 0., gradient)
                    if float(projected.abs().max()) <= 1e-12:
                        converged = True
                        message = "projected gradient converged"
                        break
                    try:
                        optimizer.step(closure) if algorithm == "torch-lbfgs" else optimizer.step()
                    except InvalidTrial:
                        # L-BFGS 的内部线搜索可能停在非法试点；清空历史，以已验证点的负梯度重试。
                        optimizer.state.clear()
                        out_state["rejected_steps"] += 1
                        budget.result["rejected_steps"] = out_state["rejected_steps"]
                        with torch.no_grad():
                            param.copy_(before - projected / max(1., float(projected.abs().max())))
                    current = accept_step(before, param.detach().clone(), optimizer)
                    if current is None and algorithm == "torch-lbfgs":
                        # 零步或上升步不算接受；仍在 Torch 内用有界负梯度方向回溯。
                        direction = -projected / max(1., float(projected.abs().max()))
                        current = accept_step(before, before + direction, optimizer)
                    if current is None:
                        message = "no improving admissible step"
                        break
                message = message or "step budget reached"
            budget.result["converged"] = converged
    except TorchUnavailable:
        # 仅在尚未接受优化步时回退；中途变化的脚本属于执行错误，不重启另一套优化。
        if out_state["accepted_steps"]:
            raise SaosError(ErrorCode.SCRIPT_ERROR, "Torch compatibility changed after an accepted step; no fallback was started")
        raise
    finally:
        guard.close()
        b._torch_device = None
        b._torch_state = None
    final = budget.result["final_merit"]
    return {"initial_merit": initial, "final_merit": final, "variables": len(problem.keys),
            "targets": len(rows) + (script is not None), "stopped_by": None,
            **outcome(initial, final, converged=converged, stopped_by=None),
            "elapsed_s": round(time.monotonic() - started, 3), "algorithm": algorithm,
            "evaluations": out_state["evaluations"], "device": chosen_device, "inner_loop": "torch",
            "message": message, "accepted_steps": out_state["accepted_steps"], "rejected_steps": out_state["rejected_steps"],
            "backtracks": guard.backtracks, "step_guard_reason": guard.last_reason,
            "min_valid_fraction": MIN_VALID_FRACTION, "max_backtracks": 0 if algorithm == "torch-trf" else TORCH_MAX_BACKTRACKS,
            "admissible_result": budget.best is not None, "ray_penalty": "differentiable_domain"}
