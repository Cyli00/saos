"""工作波段材料搜索；ZOS 负责折射率求值与局部优化。

按有效 n/V 距离挑选候选，优先尝试异常部分色散材料；先满足可行性条件，
再用带提前停止的局部优化比较性能，并立即更新后续候选的最佳起点。
surfaces="model" 选中全部模型玻璃面，用于把专利起点（library/patents）的代码玻璃换成在产牌号。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import math
from pathlib import Path
import numpy as np

from saos import glasscat
from saos.budget import Stop
from saos.protocol import ErrorCode, SaosError
from ._session import (Session, Rejected, editable, feasible, integer, merit, number,
                       optimize, references, sequential)

# 厂家目录的部分色散 P_g,F = (n_g - n_F)/(n_F - n_C) 用的谱线（µm）
G_LINE, F_LINE, C_LINE = .4358343, .4861327, .6562725
PARTIALS = ("working", "gF")


@dataclass(frozen=True)
class GlassCandidate:
    catalog: str
    name: str
    n: float
    v: float
    partial: float
    status: str = "Standard"

    def __post_init__(self):
        if not self.name or not self.catalog or any(not math.isfinite(x) for x in (self.n, self.v, self.partial)):
            raise ValueError("glass identity and finite spectral coordinates are required")


def special_glasses(glasses):
    """在 V/partial 平面拟合正常色散直线，按垂距取最远的 20%。"""
    if not glasses:
        return set()
    x = np.asarray([g.v for g in glasses], dtype=float)
    y = np.asarray([g.partial for g in glasses], dtype=float)
    denominator = len(x)*float(x@x)-float(x.sum())**2
    if denominator == 0:
        slope, intercept = -1e9, 1e9
    else:
        slope = (len(x)*float(x@y)-float(x.sum())*float(y.sum()))/denominator
        intercept = (float(x@x)*float(y.sum())-float(x.sum())*float(x@y))/denominator
    distances = np.abs(slope*x-y+intercept)/math.sqrt(slope*slope+1)
    count = int(.2*len(glasses)+.5)
    order = np.argsort(-distances, kind="stable")[:count]
    return {glasses[i].name.upper() for i in order}


def candidate_window(glasses, n, v, neighbors=7, *, special=(), feasibility_phase=False):
    """含端点的候选窗口：通常包含当前材料和指定数量的邻居，并优先安排异常色散材料。"""
    integer(neighbors, "neighbors")
    special = set(special)
    scored = []
    for g in glasses:
        d = (abs(n)-abs(g.n))**2+(abs(v/100)-abs(g.v/100))**2
        if not feasibility_phase and g.name.upper() in special:
            d = -d
        scored.append((d, g))
    scored.sort(key=lambda x: x[0])
    nearest = next((i for i,(d,_) in enumerate(scored) if d >= 0), None)
    if nearest is None:
        # 窗口需要普通候选作为距离基准；不能将全是 special 的集合当作普通 top-k。
        raise ValueError("no nonnegative-distance glass remains in the candidate database")
    end = min(nearest+neighbors, len(scored)-1)
    if feasibility_phase:
        return scored[nearest:end+1]
    maximum = scored[end][0]
    start = max(nearest-3, 0)
    while start > 0 and abs(scored[start][0]) <= maximum:
        start -= 1
    if start < nearest-3 and abs(scored[start][0]) > maximum:
        start += 1
    return scored[start:end+1]


class GlassExpert:
    def __init__(self, ctx, surfaces, *, catalogs=None, directory=None, materials=None,
                 coordinate_wavelengths_um=None, coordinates=None, optimizer=None, feasible=None,
                 candidate_filter=None, element_power=None, substitute=None, neighbors=7,
                 single_optimization_pass=False, max_passes=5, max_seconds=120,
                 cycles="auto", cores=None, min_improvement=1e-12, partial="working"):
        editable(ctx)
        self.ctx = ctx
        self.refs = [surfaces] if isinstance(surfaces, (str, int)) else list(surfaces)
        if partial not in PARTIALS:
            raise SaosError(ErrorCode.BAD_ARGS, f"partial must be one of {PARTIALS}")
        self.partial = partial
        self._databases = {}
        # 坐标各由哪边算：{"rtc": 片数, "opticstudio": 片数}；materials 给定时为 None
        self.sources = None
        if isinstance(catalogs, str):
            catalogs = [catalogs]
        self.catalogs, self.directory, self.materials = catalogs, directory, materials
        self.wavelengths = coordinate_wavelengths_um
        if self.wavelengths is not None:
            if len(self.wavelengths) != 3:
                raise SaosError(ErrorCode.BAD_ARGS, "coordinate_wavelengths_um needs long/reference/short wavelengths")
            for w in self.wavelengths:
                number(w, "coordinate wavelength", positive=True)
            if not self.wavelengths[0] > self.wavelengths[2]:
                raise SaosError(ErrorCode.BAD_ARGS, "the long coordinate wavelength must exceed the short one")
        self.coordinates_callback, self.optimizer, self.check = coordinates, optimizer, feasible
        self.candidate_filter, self.power, self.substitute = candidate_filter, element_power, substitute
        self.neighbors = integer(neighbors, "neighbors")
        self.max_passes = integer(max_passes, "max_passes")
        if not isinstance(single_optimization_pass, bool):
            raise SaosError(ErrorCode.BAD_ARGS, "single_optimization_pass must be bool")
        self.single = single_optimization_pass
        self.seconds, self.cycles, self.cores = max_seconds, cycles, cores
        self.tolerance = number(min_improvement, "min_improvement", nonnegative=True)

    @contextmanager
    def _band(self):
        """临时把前几个波长设为坐标波长（不足时追加），退出时精确恢复数值并删掉追加项。
        批量测折射率时只切换一次；不改变温度、材料模型或目录色散公式。"""
        waves = self.ctx.system.SystemData.Wavelengths
        count = int(waves.NumberOfWavelengths)
        original = [float(waves.GetWavelength(i).Wavelength) for i in range(1, count+1)]
        band = self._band_waves()
        try:
            while int(waves.NumberOfWavelengths) < len(band):
                waves.AddWavelength(band[1], 0.)
            for i, value in enumerate(band, 1):
                waves.GetWavelength(i).Wavelength = value
            self.ctx.cache.clear()
            yield len(band)
        finally:
            for i, value in enumerate(original, 1):
                waves.GetWavelength(i).Wavelength = value
            while int(waves.NumberOfWavelengths) > count:
                waves.RemoveWavelength(int(waves.NumberOfWavelengths))
            self.ctx.cache.clear()

    def _band_waves(self):
        return list(self.spectral_waves) + ([G_LINE, F_LINE, C_LINE] if self.partial == "gF" else [])

    def _measure(self, surface, count):
        """在 _band 内用 INDX 实测。/ Measured with INDX inside _band."""
        return self._from_indices([abs(float(self.ctx.op("INDX", surface, i))) for i in range(1, count+1)])

    def _from_indices(self, indices):
        """按 _band_waves 顺序的折射率 → (n, V, 部分色散)；V 与默认部分色散按工作波段，partial="gF" 时按厂家的 g、F、C 线。"""
        if any(not math.isfinite(n) or n <= 1 for n in indices):
            raise Rejected("material is invalid at a coordinate wavelength")
        long, reference, short = indices[:3]
        dispersion = short-long or 1e-9  # 避免零色散导致除零
        if self.partial == "gF":
            g, f, c = indices[3:]
            partial = (g-f)/(f-c or 1e-9)
        else:
            partial = (reference-long)/dispersion
        return reference, (reference-1)/dispersion, partial

    def _coordinates(self, surface):
        if self.coordinates_callback is not None:
            values = tuple(float(x) for x in self.coordinates_callback(self.ctx, surface))
            if len(values) != 3 or not all(math.isfinite(x) for x in values):
                raise Rejected("coordinates(ctx, surface) must return finite n, V, partial dispersion")
            return values
        with self._band() as count:
            return self._measure(surface, count)

    def _set(self, surface, glass):
        if self.substitute is not None:
            self.substitute(self.ctx, surface, glass)
        else:
            # 模型/替换求解先固定，避免局部优化器又自行改变已由外层选中的材料。
            self.ctx.backend.set_solve(surface, "material", {"type": "fixed"})
            self.ctx.set({f"{surface}.material": glass.name})
        actual = self.ctx.get(f"{surface}.material")
        if not isinstance(actual, str) or actual.upper() != glass.name.upper():
            raise SaosError(ErrorCode.VALUE_NOT_APPLIED, f"surface {surface}: glass {glass.name} was not applied")
        self.ctx.cache.clear()

    def _database(self, session, surface):
        if self.materials is not None:
            database = list(self.materials)
            if not database or not all(isinstance(g, GlassCandidate) for g in database):
                raise SaosError(ErrorCode.BAD_ARGS, "materials must be a nonempty list of GlassCandidate")
            return database, []
        catalogs = self.catalogs
        if catalogs is None:
            preferred = self.ctx.backend.substitute_catalog(None)
            catalogs = [preferred] if preferred else self.ctx.backend.system_info()["material_catalogs"]
        folder = Path(self.directory) if self.directory else (
            self.ctx.backend._glass_dir() if hasattr(self.ctx.backend, "_glass_dir") else glasscat.DEFAULT_GLASSCAT_DIR)
        # rtc 实例（ADR-0017）：目录加进 GCAT，全部牌号在 rtc 材料层算 / the rtc instance adds catalogs to GCAT and computes every glass on rtc
        rtc_instance = getattr(self.ctx.backend, "standalone", False)
        # 目录设置随镜头事务保存；不永久安装或改写厂家目录。
        if rtc_instance:
            for catalog in catalogs:
                self.ctx.backend._use_catalog(str(catalog).upper())
        else:
            mc = self.ctx.system.SystemData.MaterialCatalogs
            for catalog in catalogs:
                if not mc.IsCatalogInUse(catalog):
                    mc.AddCatalog(catalog)
                if not mc.IsCatalogInUse(catalog):
                    raise SaosError(ErrorCode.BAD_ARGS, f"OpticStudio cannot enable catalog {catalog}")
        # 坐标只取决于材料、坐标波长与环境，同一个实例里 candidates() 与 run() 复用
        key = (tuple(catalogs), str(folder), self.spectral_waves, self.partial)
        if key in self._databases:
            database, rejected, self.sources = self._databases[key]
            return database, rejected
        source, seen = [], set()
        for g in glasscat.load_catalogs(list(catalogs), folder):
            if g.status in glasscat.IN_PRODUCTION and g.name.upper() not in seen:
                seen.add(g.name.upper())
                source.append(g)
        database, rejected = [], []
        # 已准入的色散路径在 rtc 材料层直接算（与 INDX 差 ≤ 1e-8，ADR-0015），不动镜头
        from saos.backend.rtc import catalog_indices

        if rtc_instance:
            e = self.ctx.backend.system_info()["environment"]
            environment = (float(e["temperature"]), float(e["pressure"]), bool(e["adjust_index"]))
        else:
            env = self.ctx.system.SystemData.Environment
            environment = (float(env.Temperature), float(env.Pressure), bool(env.AdjustIndexToEnvironment))
        computed, left = catalog_indices([g.name for g in source], catalogs, folder, environment, self._band_waves(),
                                         admitted_only=not rtc_instance)
        rest = []
        for g in source:
            if g.name not in computed:
                if rtc_instance:
                    # rtc 实例上没有 OpticStudio 可以补测 / no OpticStudio to measure the rest on the rtc instance
                    rejected.append({"glass": g.name, "reason": left.get(g.name, "rtc cannot compute this glass")})
                else:
                    rest.append(g)
                continue
            try:
                n, v, partial = self._from_indices(computed[g.name])
                database.append(GlassCandidate(g.catalog, g.name, n, v, partial, g.status))
            except Rejected as exc:
                rejected.append({"glass": g.name, "reason": str(exc)})
        # 其余逐片换料后用 INDX 测：波长只切换一次，镜头只在最后恢复一次
        if rest:
            base = session.save()
            try:
                with self._band() as count:
                    for g in rest:
                        session.budget.check()
                        try:
                            self._set(surface, GlassCandidate(g.catalog, g.name, g.nd, g.vd, 0., g.status))
                            n, v, partial = self._measure(surface, count)
                            database.append(GlassCandidate(g.catalog, g.name, n, v, partial, g.status))
                        except Rejected as exc:
                            rejected.append({"glass": g.name, "reason": str(exc)})
            finally:
                session.restore(base)
        self.sources = {"rtc": len(computed), "opticstudio": len(rest)}
        self._databases[key] = database, rejected, self.sources
        return database, rejected

    def _order(self, surfaces):
        def power(i):
            if self.power is not None:
                return abs(number(self.power(self.ctx, i), "element_power"))
            # 共轴厚透镜的一阶光焦度；复杂系统可通过 element_power 提供排序值。
            b = self.ctx.backend
            wave = b.primary_wave()
            n0 = abs(float(self.ctx.op("INDX", i-1, wave)))
            n1 = abs(float(self.ctx.op("INDX", i, wave)))
            n2 = abs(float(self.ctx.op("INDX", i+1, wave)))
            r1, r2 = float(self.ctx.get(f"{i}.radius")), float(self.ctx.get(f"{i+1}.radius"))
            c1 = 0. if math.isinf(r1) or r1 == 0 else 1/r1
            c2 = 0. if math.isinf(r2) or r2 == 0 else 1/r2
            p1, p2 = (n1-n0)*c1, (n2-n1)*c2
            if not all(math.isfinite(n) and n > 0 for n in (n0,n1,n2)):
                raise Rejected("invalid refractive indices for element ordering")
            return abs(p1+p2-float(self.ctx.get(f"{i}.thickness"))/n1*p1*p2)
        return sorted(surfaces, key=lambda i: -power(i))

    def _options(self, database, special, surface, *, initial=False):
        n, v, _ = self._coordinates(surface)
        allowed = [g for g in database if self.candidate_filter is None or self.candidate_filter(self.ctx, surface, g)]
        if not allowed:
            return []
        return candidate_window(allowed, n, v, self.neighbors, special=special, feasibility_phase=initial)

    def candidates(self, surface):
        """只列候选，退出时恢复当前镜头；会按需在临时镜头态测量工作波段折射率。"""
        editable(self.ctx)
        info = sequential(self.ctx)
        self._spectral(info)
        i = references(self.ctx, [surface])[0]
        with Session(self.ctx, self.seconds) as session:
            try:
                database, rejected = self._database(session, i)
                special = special_glasses(database)
                return {"candidates": [{"distance_squared": d, **asdict(g)} for d,g in self._options(database, special, i)],
                        "special": sorted(special), "rejected": rejected,
                        "coordinate_wavelengths_um": self.spectral_waves, "partial": self.partial,
                        "coordinate_sources": self.sources, "backend": self.ctx.backend.identity()}
            except Stop:
                return {"candidates": [], "stopped_by": session.budget.stopped_by,
                        "coordinate_wavelengths_um": self.spectral_waves}

    def _spectral(self, info):
        waves = info["wavelengths"]
        primary = next(w["um"] for w in waves if w["primary"])
        all_waves = [w["um"] for w in waves]
        self.spectral_waves = tuple(self.wavelengths or (
            (max(all_waves), primary, min(all_waves)) if max(all_waves)>min(all_waves)
            else (.6562725, .5875618, .4861327)))

    def _optimize(self, session, best):
        threshold = -math.inf
        for iteration in range(1, 2 if self.single else 5):
            value = optimize(self.ctx, self.optimizer, session.budget, cycles=self.cycles, cores=self.cores)
            if self.single:
                break
            if iteration == 1:
                threshold = value-.333*(value-best)
            elif iteration in (2, 3):
                if value > threshold:
                    break
                if iteration == 2:
                    threshold = value-.5*(value-best)
        return value

    def _surfaces(self):
        """面号；"model" 选中所有以模型玻璃求解（MaterialModel）表示的面，例如专利起点里由玻璃代码转来的材料。"""
        if self.refs == ["model"]:
            found = [s["index"] for s in self.ctx.backend.surfaces()[1:-1] if s["solves"].get("material") == "MaterialModel"]
            if not found:
                raise SaosError(ErrorCode.BAD_ARGS, "surfaces='model': the lens has no model-glass surfaces")
            return found
        return references(self.ctx, self.refs)

    def run(self):
        editable(self.ctx)
        info = sequential(self.ctx)
        self._spectral(info)
        surfaces = self._surfaces()
        table = self.ctx.backend.surfaces()
        for i in surfaces:
            material = self.ctx.get(f"{i}.material")
            if i+1 >= len(table)-1 or material in ("", "AIR", "MIRROR", "ABSORB"):
                raise SaosError(ErrorCode.BAD_ARGS, f"surface {i} is not a supported material segment with a real back surface")
        events, accepted = [], []
        status, complete = "max_passes", False
        with Session(self.ctx, self.seconds) as session:
            initial_merit = merit(self.ctx)
            best, best_merit = session.entry, initial_merit
            try:
                database, rejected = self._database(session, surfaces[0])
                if not database:
                    raise Rejected("no in-production glass is valid in the coordinate band")
                best = session.save()  # 保留启用的目录，但不保留探测时的换料
                special = special_glasses(database)
                acceptable = set()
                # 先满足材料可行性条件，此阶段不以 merit 改善作为接受门槛。
                for _ in range(self.max_passes):
                    found = False
                    for surface in self._order(surfaces):
                        session.budget.check()
                        if surface in acceptable:
                            continue
                        current = self.ctx.get(f"{surface}.material")
                        for distance, g in self._options(database, special, surface, initial=True):
                            session.restore(best)
                            try:
                                self._set(surface, g)
                                feasible(self.check, self.ctx, surface)
                            except Rejected:
                                continue
                            best = session.save()
                            acceptable.add(surface)
                            found = True
                            events.append({"phase": "feasibility", "surface": surface, "glass": g.name,
                                           "changed": not isinstance(current, str) or current.upper() != g.name.upper()})
                            break
                        session.restore(best)
                    if len(acceptable) == len(surfaces):
                        best_merit = optimize(self.ctx, self.optimizer, session.budget,
                                              cycles=self.cycles, cores=self.cores)
                        for surface in surfaces:
                            feasible(self.check, self.ctx, surface)
                        best = session.save()
                        complete = True
                        break
                    if not found:
                        raise Rejected("no acceptable material set found within the candidate windows")
                if not complete:
                    raise Rejected("material feasibility pass limit reached")
                for sweep in range(1, self.max_passes+1):
                    changed = False
                    for surface in self._order(surfaces):
                        session.budget.check()
                        options = self._options(database, special, surface)
                        for distance, g in options:
                            session.budget.check()
                            session.restore(best)
                            current = self.ctx.get(f"{surface}.material")
                            try:
                                self._set(surface, g)
                                feasible(self.check, self.ctx, surface)
                                value = self._optimize(session, best_merit)
                                feasible(self.check, self.ctx, surface)
                            except Rejected as exc:
                                events.append({"phase": "optimization", "surface": surface, "glass": g.name,
                                               "accepted": False, "reason": str(exc)})
                                continue
                            improved = value < best_merit-self.tolerance*max(1., abs(best_merit))
                            record = {"phase": "optimization", "pass": sweep, "surface": surface,
                                      "glass": g.name, "catalog": g.catalog, "special": g.name.upper() in special,
                                      "distance_squared": distance, "merit": value, "accepted": improved}
                            events.append(record)
                            if improved:
                                material_changed = not isinstance(current, str) or current.upper() != g.name.upper()
                                changed |= material_changed
                                best_merit, best = value, session.save()
                                accepted.append(dict(record, material_changed=material_changed))
                                # 立即以新 best 为后续候选的起点，不能恢复成元件入口快照。
                        session.restore(best)
                    if not changed:
                        status = "no_glass_changed"
                        break
            except Stop:
                status = session.budget.stopped_by
            except Rejected as exc:
                status = "infeasible"
                events.append({"reason": str(exc)})
            if complete:
                session.commit(best)
            return {"method": "sequential_glass_search", "status": status,
                    "initial_merit": initial_merit, "final_merit": best_merit if complete else initial_merit,
                    "surfaces": surfaces, "feasibility_completed": complete, "accepted": accepted, "events": events,
                    "coordinate_wavelengths_um": self.spectral_waves, "partial": self.partial,
                    "coordinate_sources": self.sources, "backend": self.ctx.backend.identity()}
