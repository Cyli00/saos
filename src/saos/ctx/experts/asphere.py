"""非球面选面与优化：当前镜头的数值灵敏度、线性投影排名及逐面 placement。

归一化、斜率预算缩放与累积投影见 ranking.py。默认面型为 EvenAspheric；
QTypeAsphere 需要显式给出经确认的参数列、归一化设置和差分步长。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import numpy as np

from saos.budget import Stop
from saos.protocol import ErrorCode, SaosError
from ._session import Session, Rejected, editable, feasible, integer, merit, number, optimize, references, sequential
from .ranking import SurfaceSensitivity, rank as rank_projection


ENGINES = ("opticstudio", "rtc")


@dataclass(frozen=True)
class AsphereProfile:
    surface_type: str = "EvenAspheric"
    coefficients: tuple[str, ...] = ("par2", "par3", "par4", "par5")
    settings: dict = field(default_factory=dict)
    steps: tuple[float, ...] | None = None


def mfe_residual(ctx):
    """由 MFE 各行的贡献百分比重构带权残差：e_i = sign(value-target)·sqrt(contribution_i/100)·merit。
    边界操作数（OPGT、OPLT、MNCT 等）满足时贡献为 0，value-target 却不为 0，所以不能按权重直接重算；
    贡献合计须为 100%，否则要求 residual(ctx)，不从单个 merit 猜雅可比。"""
    value = merit(ctx)
    # DMFS 等标记行读出的权重是 inf，不是操作数
    rows = [r for r in ctx.backend.merit_rows() if 0 < r["weight"] < math.inf]
    if not rows:
        raise SaosError(ErrorCode.BAD_ARGS, "asphere ranking needs a nonempty weighted MFE or residual(ctx)")
    share = np.asarray([r["contribution"] for r in rows], dtype=float)/100
    delta = np.asarray([r["value"]-r["target"] for r in rows], dtype=float)
    if not (np.isfinite(share).all() and np.isfinite(delta).all()):
        raise SaosError(ErrorCode.BAD_ARGS, "MFE rows hold nonfinite values; supply residual(ctx) for this MFE")
    if value == 0:
        return np.zeros(len(rows))
    if (share < -1e-9).any() or not math.isclose(float(share.sum()), 1., abs_tol=1e-6):
        raise SaosError(ErrorCode.BAD_ARGS,
                        "MFE contributions of positive-weight rows do not add up to 100%; supply residual(ctx) for this MFE")
    return np.sign(delta)*np.sqrt(np.clip(share, 0., None))*value


def _glass(value):
    return isinstance(value, dict) or str(value).strip().upper() not in ("", "AIR", "MIRROR", "ABSORB")


def topology(surfaces):
    """由空气/材料段确定连续实体的两侧及胶合界面；不按面号奇偶猜元件。"""
    groups, cemented, start, group = {}, set(), None, 0
    for i in range(1, len(surfaces)-1):
        before, after = _glass(surfaces[i-1]["material"]), _glass(surfaces[i]["material"])
        if after and start is None:
            start = i
            group += 1
        if start is not None:
            groups[i] = group
        if before and after:
            cemented.add(i)
        if before and not after:
            start = None
    return groups, cemented


class AsphereExpert:
    def __init__(self, ctx, surfaces, *, terms=4, profile=None, residual=None, optimizer=None,
                 slope_limit_waves=None, wavelength_um=None, slope_vector=None, constraints=None,
                 feasible=None, candidate_filter=None, single_sided=True, allow_successive=True,
                 exclude_cemented=True, fd_sag=1e-5, max_seconds=120, cycles="auto", cores=None,
                 engine="opticstudio", grid=7):
        editable(ctx)
        self.ctx = ctx
        if engine not in ENGINES:
            raise SaosError(ErrorCode.BAD_ARGS, f"engine must be one of {ENGINES}")
        self.engine, self.grid = engine, integer(grid, "grid", 3)
        self.refs = [surfaces] if isinstance(surfaces, (str, int)) else list(surfaces)
        terms = integer(terms, "terms")
        if terms > 14:
            raise SaosError(ErrorCode.BAD_ARGS, "terms must be in 1..14")
        self.profile = profile or AsphereProfile(coefficients=tuple(f"par{i+2}" for i in range(terms)))
        p = self.profile
        if not isinstance(p, AsphereProfile) or not 1 <= len(p.coefficients) <= 14 or len(set(p.coefficients)) != len(p.coefficients):
            raise SaosError(ErrorCode.BAD_ARGS, "profile needs 1..14 distinct coefficient keys")
        if set(p.coefficients).intersection(p.settings):
            raise SaosError(ErrorCode.BAD_ARGS, "profile settings must not also set coefficient keys")
        if p.surface_type != "EvenAspheric" and p.steps is None:
            raise SaosError(ErrorCode.BAD_ARGS, "non-default asphere profiles need explicit coefficient steps")
        if p.steps is not None:
            if len(p.steps) != len(p.coefficients):
                raise SaosError(ErrorCode.BAD_ARGS, "profile steps and coefficients must have equal lengths")
            for h in p.steps:
                number(h, "coefficient difference step", positive=True)
        self.residual = residual or mfe_residual
        self.optimizer, self.check, self.candidate_filter = optimizer, feasible, candidate_filter
        self.limit = None if slope_limit_waves is None else number(slope_limit_waves, "slope_limit_waves", positive=True)
        if self.limit is not None and constraints is None:
            raise SaosError(ErrorCode.BAD_ARGS,
                            "finite slope limits require constraints(ctx, surface, limit_waves) to persist ZOS merit constraints")
        if self.limit is not None and p.surface_type != "EvenAspheric" and slope_vector is None:
            raise SaosError(ErrorCode.BAD_ARGS, "a non-default profile needs a calibrated slope_vector(ctx, surface)")
        self.constraints, self.slope_vector = constraints, slope_vector
        if engine == "rtc":
            # rtc 排名用光线残差和 AD 雅可比：只接默认偶次非球面系数，不接 OpticStudio 侧的回调
            if p.surface_type != "EvenAspheric" or any(not k.startswith("par") or not k[3:].isdigit() or not 2 <= int(k[3:]) <= 12
                                                        for k in p.coefficients):
                raise SaosError(ErrorCode.BAD_ARGS, "engine='rtc' ranks EvenAspheric par2..par12 coefficients only")
            if residual is not None or slope_vector is not None:
                raise SaosError(ErrorCode.BAD_ARGS, "engine='rtc' ranks on rtc ray errors; residual and slope_vector "
                                                    "callbacks belong to engine='opticstudio'")
        self.wavelength = None if wavelength_um is None else number(wavelength_um, "wavelength_um", positive=True)
        self.single_sided, self.allow_successive, self.exclude_cemented = single_sided, allow_successive, exclude_cemented
        self.fd_sag = number(fd_sag, "fd_sag", positive=True)
        self.seconds, self.cycles, self.cores = max_seconds, cycles, cores

    def _candidates(self):
        surfaces = self.ctx.backend.surfaces()
        groups, cemented = topology(surfaces)
        occupied = {s["index"] for s in surfaces[1:-1]
                    if str(s["type"]).replace(" ", "").lower() != "standard" or float(s["conic"]) != 0.}
        result, excluded = [], []
        for i in sorted(references(self.ctx, self.refs)):
            s = surfaces[i]
            reason = None
            if i not in groups or i in occupied:
                reason = "not a spherical material interface"
            elif str(s["material"]).upper() in ("MIRROR", "ABSORB"):
                reason = "reflective/absorbing surfaces are not supported by this adapter"
            elif s.get("solves", {}).get("radius", "Fixed") not in ("Fixed", "Variable"):
                reason = "curvature solve/pickup"
            elif self.exclude_cemented and i in cemented:
                reason = "cemented interface"
            elif self.single_sided and any(groups.get(j) == groups[i] for j in occupied):
                reason = "single-sided preference"
            elif not self.allow_successive and any(j in occupied and
                    ((j == i-1 and not _glass(surfaces[j]["material"])) or
                     (j == i+1 and not _glass(s["material"]))) for j in (i-1, i+1)):
                reason = "successive aspheres across air"
            elif self.candidate_filter is not None and not self.candidate_filter(self.ctx, i):
                reason = "candidate_filter"
            if reason:
                excluded.append({"surface": i, "reason": reason})
            else:
                result.append(i)
        return result, excluded

    def _convert(self, surface, *, variable=False):
        ctx, p = self.ctx, self.profile
        ctx.backend.change_surface_type(surface, p.surface_type)
        ctx.backend.set_param(f"{surface}.conic", 0.)
        ctx.backend.set_solve(surface, "conic", {"type": "fixed"})
        for key, value in p.settings.items():
            ctx.backend.set_param(f"{surface}.{key}", value)
        for key in p.coefficients:
            ctx.backend.set_solve(surface, key, {"type": "fixed"})
            ctx.backend.set_param(f"{surface}.{key}", 0.)
            if variable:
                ctx.backend.set_solve(surface, key, {"type": "variable"})
        ctx.cache.clear()

    def _slopes(self, surface, coefficients=None):
        """coefficients 给定时用它代替镜头上的系数（默认度量对系数是线性的）。"""
        if self.slope_vector is not None:
            values = np.asarray(self.slope_vector(self.ctx, surface), dtype=float)
        else:
            # EvenAspheric 的 par2 起是 r^4、r^6…；度量是相对于当前基底 conic 的
            # 面积 RMS 径向斜率外推深度（waves/radius），不包含重新拟合基底 conic 的步骤。
            orders = []
            for key in self.profile.coefficients:
                if not key.startswith("par") or not key[3:].isdigit() or int(key[3:]) < 2:
                    raise SaosError(ErrorCode.BAD_ARGS, "custom coefficient keys need slope_vector(ctx, surface)")
                orders.append(2*int(key[3:]))
            radius = number(self.ctx.get(f"{surface}.semi_diameter"), "surface semi-diameter", positive=True)
            info = self.ctx.backend.system_info()
            unit = info.get("lens_units") or str(self.ctx.system.SystemData.Units.LensUnits)
            um_per_unit = {"Millimeters": 1000., "Centimeters": 10000., "Meters": 1e6,
                           "Inches": 25400., "Micrometers": 1.}.get(unit)
            if um_per_unit is None:
                raise SaosError(ErrorCode.BAD_ARGS, f"unsupported lens units for slope conversion: {unit}")
            wavelength = self.wavelength or next(w["um"] for w in info["wavelengths"] if w["primary"])
            wavelength = wavelength/um_per_unit
            x, w = np.polynomial.legendre.leggauss(32)
            r = radius*np.sqrt((x+1)/2)
            if coefficients is None:
                coefficients = [float(self.ctx.get(f"{surface}.{key}")) for key in self.profile.coefficients]
            slopes = sum(a*order*r**(order-1) for a, order in zip(coefficients, orders))
            values = slopes*radius/wavelength*np.sqrt(w/2)
        if values.ndim != 1 or not values.size or not np.isfinite(values).all():
            raise Rejected("slope vector is empty or nonfinite")
        return values

    def _residual(self):
        e = np.asarray(self.residual(self.ctx), dtype=float)
        if e.ndim != 1 or not e.size or not np.isfinite(e).all():
            raise Rejected("residual is empty or nonfinite")
        return e

    def _rank(self, session):
        return self._rank_rtc(session) if self.engine == "rtc" else self._rank_opticstudio(session)

    def _rank_rtc(self, session):
        """在 rtc 副本上排名（ADR-0015）：残差是各视场、各波长光瞳光线相对主波长主光线的横向像差，
        按视场与波长权重加权；最后一面厚度作对焦补偿从残差和雅可比里投影掉。雅可比由一次 AD 追迹得到，不做差分。
        数值只用于排名；run() 是否接受仍看 OpticStudio 上按镜头 MFE 优化后的实测。"""
        from rtc.adtrace import Variable, trace_ad

        from saos import fast as fastmod
        from saos.kit import pupil

        candidates, excluded = self._candidates()
        base = session.save()
        try:
            # 候选面先在 OpticStudio 上转成零系数的非球面（光学上不变），整份交给 rtc；副本建立时已与 OpticStudio 对照追迹。
            # rtc 实例（ADR-0017）直接用它自己的镜头 / the rtc instance uses its own lens directly
            for surface in candidates:
                self._convert(surface)
            if getattr(self.ctx.backend, "standalone", False):
                engine, agreement = self.ctx.backend, None
            else:
                copy = fastmod.from_context(self.ctx, freeze_solves=True)
                engine, agreement = copy.backend, copy.agreement
            lens = engine.lens
            primary = engine.optical_system(lens.primary_wave)
            item = {it.index: k for k, it in enumerate(primary.items)}
            image = primary.items[-1].index
            # 实际瞄准时 AD 把发射当常量：光阑前的面改变瞄准，导数会漏掉这一项
            if lens.ray_aiming == 2 and primary.stop is not None and any(item[s] < primary.stop for s in candidates):
                raise SaosError(ErrorCode.BACKEND_UNSUPPORTED,
                                "with real ray aiming, engine='rtc' cannot rank surfaces before the stop; use engine='opticstudio'")
            keys = [(s, int(key[3:])) for s in candidates for key in self.profile.coefficients]
            variables = [Variable(item[s], "param", n) for s, n in keys] + [Variable(item[image - 1], "thickness")]
            fields = fastmod.field_points(lens)
            field_weights = [float(f[2]) for f in lens.fields]
            px, py = (np.asarray(c, dtype=float) for c in zip(*pupil(self.grid)))
            values, rows = [], []
            for wave, (_, wave_weight) in enumerate(lens.active_wavelengths, 1):
                system = engine.optical_system(wave)
                theta = np.asarray([v.value(system) for v in variables], dtype=float)
                for (hx, hy), field_weight in zip(fields, field_weights):
                    session.budget.check()
                    chief = trace_ad(primary, [0.], [0.], variables, theta, hx, hy).at(image)
                    got = trace_ad(system, px, py, variables, theta, hx, hy).at(image)
                    scale = math.sqrt(max(field_weight, 0.)*max(float(wave_weight), 0.))
                    for axis in ("x", "y"):
                        r = got[axis] - chief[axis]
                        values.append(scale*np.ravel(r.v))
                        rows.append(scale*np.asarray(r.d).reshape(len(variables), -1).T)
        finally:
            session.restore(base)
        e, J = np.concatenate(values), np.vstack(rows)
        usable = np.isfinite(e) & np.isfinite(J).all(axis=1)
        e, J = e[usable], J[usable]
        if not e.size:
            raise SaosError(ErrorCode.BAD_ARGS, "no ray reached the image in the rtc copy")
        # 对焦补偿：把残差和各系数列投影到对焦列的正交补上
        focus, J = J[:, -1], J[:, :-1]
        if np.linalg.norm(focus) > 0:
            q = focus/np.linalg.norm(focus)
            e, J = e - q*(q@e), J - np.outer(q, q@J)
        models, width = [], len(self.profile.coefficients)
        for k, surface in enumerate(candidates):
            slope = current = None
            if self.limit is not None:
                eye = np.eye(width)
                slope = np.column_stack([self._slopes(surface, list(eye[j])) for j in range(width)])
                current = self._slopes(surface, [0.]*width)
            models.append(SurfaceSensitivity(surface, J[:, k*width:(k+1)*width], slope, current, self.limit))
        result = rank_projection(e, models)
        result.update(excluded=excluded, surface_type=self.profile.surface_type, engine="rtc",
                      residual="transverse ray errors about the primary chief ray, focus compensated", residual_rows=int(usable.sum()),
                      backend=self.ctx.backend.identity(), rtc=engine.identity(), agreement=agreement,
                      slope_metric="base_conic_radial_rms_waves_per_radius")
        return result

    def _rank_opticstudio(self, session):
        candidates, excluded = self._candidates()
        base = session.save()
        nominal = self._residual()
        models = []
        for surface in candidates:
            session.budget.check()
            session.restore(base)
            try:
                self._convert(surface)
                zero = session.save()
                e = self._residual()
                if e.shape != nominal.shape or not np.allclose(e, nominal, rtol=1e-7, atol=1e-10):
                    raise Rejected("zero-coefficient conversion changed the nominal residual")
                current_slope = self._slopes(surface) if self.limit is not None else None
                radius = number(self.ctx.get(f"{surface}.semi_diameter"), "semi-diameter", positive=True)
                steps = self.profile.steps or tuple(self.fd_sag/radius**(2*int(key[3:])) for key in self.profile.coefficients)
                J, L = [], []
                for key, h in zip(self.profile.coefficients, steps):
                    session.budget.check()
                    number(h, "difference step", positive=True)
                    observations = []
                    for sign in (1., -1.):
                        session.budget.check()
                        session.restore(zero)
                        self.ctx.backend.set_param(f"{surface}.{key}", sign*h)
                        self.ctx.cache.clear()
                        point = self._residual()
                        if point.shape != nominal.shape:
                            raise Rejected("residual rows changed during differentiation")
                        observations.append((point, self._slopes(surface) if self.limit is not None else None))
                    J.append((observations[0][0]-observations[1][0])/(2*h))
                    if self.limit is not None:
                        L.append((observations[0][1]-observations[1][1])/(2*h))
                models.append(SurfaceSensitivity(surface, np.column_stack(J),
                              np.column_stack(L) if L else None, current_slope, self.limit))
            except Rejected as exc:
                excluded.append({"surface": surface, "reason": str(exc)})
            finally:
                session.restore(base)
        result = rank_projection(nominal, models)
        result["excluded"] = excluded
        result["surface_type"] = self.profile.surface_type
        result["backend"] = self.ctx.backend.identity()
        result["slope_metric"] = "caller_calibrated" if self.slope_vector else "base_conic_radial_rms_waves_per_radius"
        result["engine"] = "opticstudio"
        return result

    def rank(self):
        """分析模式：完整恢复镜头、求解、MFE 和原 dirty 标志；不运行局部优化。"""
        editable(self.ctx)
        sequential(self.ctx)
        with Session(self.ctx, self.seconds) as session:
            try:
                return self._rank(session)
            except Stop:
                return {"method": "linear_projection", "stopped_by": session.budget.stopped_by,
                        "order": [], "selected": [], "rounds": []}

    def run(self, nas=1):
        """每接受一个面后重算灵敏度；nas=0 仅排名。成功保留接受态，不自动保存用户镜头。"""
        integer(nas, "nas", 0)
        if nas == 0:
            return self.rank()
        editable(self.ctx)
        sequential(self.ctx)
        history, installed = [], []
        with Session(self.ctx, self.seconds) as session:
            best = session.entry
            status = "requested_count_reached"
            try:
                for _ in range(nas):
                    session.budget.check()
                    ranking = self._rank(session)
                    history.append(ranking)
                    if not ranking["selected"] or ranking["selected"][0]["improvement_factor"] <= 0:
                        status = "no_predicted_gain"
                        break
                    candidate = ranking["selected"][0]
                    surface = candidate["surface"]
                    original_merit = merit(self.ctx)
                    self._convert(surface, variable=True)
                    if self.constraints is not None:
                        self.constraints(self.ctx, surface, self.limit)
                    # 新增约束会改变 MFE 的归一化，必须在同一个修改后 MFE 上比较前后。
                    before = merit(self.ctx)
                    for key, value in zip(self.profile.coefficients, candidate["step"]):
                        self.ctx.backend.set_param(f"{surface}.{key}", value)
                    after = optimize(self.ctx, self.optimizer, session.budget,
                                     cycles=self.cycles, cores=self.cores)
                    feasible(self.check, self.ctx, surface)
                    if self.limit is not None:
                        for i in [r["surface"] for r in installed]+[surface]:
                            if np.linalg.norm(self._slopes(i)) > self.limit*(1+1e-8):
                                raise Rejected(f"surface {i} exceeds its slope limit after optimization")
                    if after >= before - 1e-12*max(1., abs(before)):
                        status = "no_actual_gain"
                        session.restore(best)
                        break
                    installed.append({"surface": surface, "original_merit": original_merit,
                                      "before": before, "after": after})
                    best = session.save()
            except Stop:
                status = session.budget.stopped_by
            except Rejected as exc:
                status = "rejected"
                history.append({"reason": str(exc)})
            if installed:
                session.commit(best)
            return {"method": "linear_projection", "status": status,
                    "installed": installed, "rankings": history,
                    "backend": self.ctx.backend.identity()}
