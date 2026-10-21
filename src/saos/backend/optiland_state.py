from __future__ import annotations

import copy
import math
from contextlib import contextmanager

import numpy as np

from saos import config, timing
from saos.backend.base import check_size, is_par, parse_numeric, same_number
from saos.backend.optiland_materials import (
    MaterialStore,
    environment_material,
    expansion_factor,
    unsupported,
)
from saos.backend.optiland_surfaces import (
    DISPLAY,
    aperture,
    aperture_description,
    geometry_kwargs,
    kind,
    parameters,
    position_surfaces,
)
from saos.protocol import ErrorCode, SaosError

APERTURES = {"entrance_pupil_diameter": "EPD", "image_f_number": "imageFNO", "object_na": "objectNA",
             "float_by_stop": "float_by_stop_size", "object_cone_angle": "objectNA"}
CONFIG_OPERANDS = {"THIC", "CRVT", "GLSS", "CONI", "SDIA", "PRAM", "XFIE", "YFIE", "WAVE", "APER", "TEMP", "PRES"}


def array(value):
    import optiland.backend as be

    return np.asarray(be.to_numpy(value), dtype=np.float64)


class OpticalState:
    def _initialize_state(self):
        self.started = False
        self.file = None
        self.optic = None
        self._prescription = None
        self._effective = None
        self._rows = []
        self._store = MaterialStore()
        self._configuration = 1
        self._revision = 0
        self._synchronized = None
        self._aiming = "paraxial"
        self._analysis_cache = {}
        self._merit = []
        self._merit_script = None
        self._merit_cache = None
        self._bounds = {}
        self._glass_vary = []
        self._torch_device = None
        self._torch_state = None
        self._scales = {}
        self._last_path = {}
        self._last_opd = {}

    @staticmethod
    def can_handle(prescription):
        p, reasons = prescription, []
        if p.get("mode", "sequential") != "sequential":
            reasons.append("non-sequential objects require ZOS")
        if p.get("units", "mm") != "mm":
            reasons.append("non-millimetre prescriptions require ZOS")
        if p.get("aperture", {}).get("type") not in APERTURES:
            reasons.append("unsupported system aperture")
        if p.get("fields", {}).get("type") not in ("angle", "object_height", "paraxial_image_height", "real_image_height"):
            reasons.append("unsupported field definition")
        for key in ("afocal", "polarization", "coating_file"):
            if p.get(key):
                reasons.append(f"{key} requires ZOS")
        if (p.get("apodization") or {}).get("type", "uniform") != "uniform":
            reasons.append("pupil apodization conventions have not been aligned with OpticStudio")
        extra = set(p.get("ray_aiming_options") or {}) - {"robust", "cache"}
        if extra:
            reasons.append(f"ray aiming options {sorted(extra)} require ZOS")
        for row in p.get("configurations", []):
            if row["operand"] not in CONFIG_OPERANDS:
                reasons.append(f"configuration operand {row['operand']} requires ZOS")
            elif row["operand"] == "GLSS" and any(isinstance(v, dict) and v.get("model", {}).get("dpgf") for v in row["values"]):
                reasons.append("configuration GLSS with a nonzero-ΔPgF model glass requires ZOS")
        for i, row in enumerate([p.get("object", {}), *p.get("surfaces", []), p.get("image") or {}]):
            if row.get("objects"):
                reasons.append(f"surface {i}: mixed-mode objects require ZOS")
            if row.get("coating"):
                reasons.append(f"surface {i}: coating definitions require ZOS")
            if isinstance(row.get("material"), dict) and row["material"].get("model", {}).get("dpgf"):
                reasons.append(f"surface {i}: a model glass with a nonzero ΔPgF has no Optiland approximation")
            try:
                geometry_kwargs(row)
                if row.get("aperture", {}).get("type") == "spider":
                    reasons.append(f"surface {i}: spider apertures require ZOS")
            except SaosError as exc:
                reasons.append(f"surface {i}: {exc.message}")
        return {"supported": not reasons, "backend": "optiland" if not reasons else "zos", "reasons": reasons}

    def _configure(self):
        return self._configure_now()

    def _configure_now(self):
        import optiland.backend as be

        # torch 优化运行期间（_torch_device 不为空）整个 Optic 用 torch 后端，结束后换回
        torch_device = getattr(self, "_torch_device", None)
        wanted = "torch" if torch_device or self.device != "cpu" else "numpy"
        if be.get_backend() != wanted:
            be.set_backend(wanted)
        if wanted == "torch":
            be.set_device(torch_device or self.device)
        be.set_precision(config.COMPUTE_PRECISION)
        return be

    def close(self):
        self._initialize_state()

    def healthy(self):
        return self.started

    def new_system(self):
        started = self.started
        self._initialize_state()
        self.started = started

    def _require(self):
        if self._prescription is None:
            raise SaosError(ErrorCode.NO_LENS_LOADED, "no lens loaded")
        return self._prescription

    def _invalidate(self):
        self._revision += 1
        self._analysis_cache.clear()
        self._last_path.clear()
        self._last_opd.clear()

    def _configuration_prescription(self):
        p = copy.deepcopy(self._require())
        rows = [p["object"], *p["surfaces"], p.setdefault("image", {"type": "standard", "radius": math.inf})]
        for spec in p.get("configurations", []):
            op, args, value = spec["operand"], [*spec["params"], 0, 0, 0], spec["values"][self._configuration - 1]
            if op in ("THIC", "CRVT", "GLSS", "CONI", "SDIA", "PRAM"):
                if not 0 <= args[0] < len(rows):
                    raise SaosError(ErrorCode.BAD_ARGS, f"configuration surface {args[0]} out of range")
                row = rows[args[0]]
                if op == "PRAM":
                    row.setdefault("params", {})[f"par{args[1]}"] = value
                else:
                    key = {"THIC": "thickness", "CRVT": "radius", "GLSS": "material", "CONI": "conic", "SDIA": "semi_diameter"}[op]
                    row[key] = (math.inf if value == 0 else 1 / value) if op == "CRVT" else value
            elif op in ("XFIE", "YFIE", "WAVE"):
                sequence = p["wavelengths"]["um"] if op == "WAVE" else p["fields"]["x" if op == "XFIE" else "y"]
                if not 1 <= args[0] <= len(sequence):
                    raise SaosError(ErrorCode.BAD_ARGS, f"configuration {op} index out of range")
                sequence[args[0] - 1] = value
            elif op == "APER":
                p["aperture"]["value"] = value
            else:
                p["environment"]["temperature" if op == "TEMP" else "pressure"] = value
        return p

    def _expand_thermal(self, p, rows):
        settings = p.get("thermal") or {}
        reference = settings.get("reference_temperature", 20.0)
        temperature = p["environment"]["temperature"]
        scales = {}
        # OpticStudio 的 TEMP/PRES 只调折射率，半径和厚度随温度变化要靠热拾取求解；处方没有 thermal 设置时不展开，两端才一致
        if not settings or temperature == reference:
            return scales
        sources = [self._store.get(row.get("material", ""), p["catalogs"]) if str(row.get("material", "")).upper() != "MIRROR"
                   else None for row in rows]
        for i, row in enumerate(rows):
            if kind(row) == "coordinatebreak":
                continue
            glass = row.get("material") and str(row.get("material")).upper() not in ("AIR", "MIRROR")
            previous_glass = i and rows[i - 1].get("material") and str(rows[i - 1]["material"]).upper() not in ("AIR", "MIRROR")
            host = sources[i] if glass else sources[i - 1] if previous_glass else None
            override = settings.get("surfaces", {}).get(str(i))
            radial = expansion_factor(host, reference, temperature, override=override) if host is not None or override else 1.0
            spacer = settings.get("spacers", {}).get(str(i))
            axial = expansion_factor(sources[i], reference, temperature) if glass else (
                expansion_factor(None, reference, temperature, mechanical=spacer) if spacer else 1.0)
            for key, factor in (("radius", radial), ("thickness", axial), ("semi_diameter", radial), ("mech_semi_diameter", radial)):
                if key in row:
                    row[key] *= factor
                    scales[(i, key)] = factor
            params = row.get("params", {})
            k = kind(row)
            for key in list(params):
                n = int(key[3:]) if is_par(key) else None
                factor = 1.0
                if k in ("evenaspheric", "oddaspheric") and n is not None:
                    factor = radial ** (1 - (2 * n if k == "evenaspheric" else n))
                elif k.startswith("zernike") and n is not None:
                    factor = radial ** (1 - 2 * n) if 1 <= n <= 8 else radial if n in (9, 10, 14) or n >= 15 else 1
                elif k == "toroidal" and n is not None:
                    factor = radial if n == 1 else radial ** (1 - 2 * (n - 1)) if 2 <= n <= 8 else 1
                elif k == "chebyshevpolynomial":
                    factor = radial if n in (3, 4) or key.lower().startswith("c") else 1
                elif k == "qtypeasphere":
                    factor = radial if n is not None and n >= 15 else 1
                elif k == "qtypefreeform":
                    factor = radial if n in (1, 2, 4) or key.startswith(("a(", "b(")) else 1
                elif k == "diffractiongrating" and n == 1:
                    factor = 1 / radial
                params[key] *= factor
                scales[(i, key)] = factor
        return scales

    def _construct(self, p, rows):
        from optiland.optic import Optic

        self._configure()
        optic = Optic(p.get("title", ""))
        stop = next((i for i, row in enumerate(rows) if row.get("stop")), 1)
        env = p["environment"]
        for i, row in enumerate(rows):
            surface_type, kwargs = geometry_kwargs(row)
            if kind(row) == "coordinatebreak":
                material = optic.surfaces[i - 1].material_post
            elif str(row.get("material", "")).upper() == "MIRROR":
                material = "mirror"
            else:
                material = environment_material(self._store.get(row.get("material", ""), p["catalogs"]),
                                                env["temperature"], env["pressure"], env["adjust_index"])
            # 处方给定的半口径（OpticStudio 的 Fixed）会裁光线：2026-10-20 实测 Cooke 三片镜第 1、4、6 面给小半口径时
            # OpticStudio 通光率降到 0.15–0.38；面孔径优先于半口径。自动半口径在此之后才算，包住全部光线，不裁
            kwargs.update(thickness=row.get("thickness", 0.0), aperture=aperture(row.get("aperture"), row.get("semi_diameter") if i else None))
            optic.surfaces.add(index=i, surface_type=surface_type, material=material, comment=row.get("comment", ""),
                               is_stop=i == stop, **kwargs)
            optic.surfaces[i].semi_aperture = row.get("semi_diameter")
        optic.saos_axes = position_surfaces(optic, rows)
        ap = p["aperture"]
        # Optiland 的 float_by_stop_size 取光阑直径（传半径时入瞳直径恰好是 OpticStudio 的一半）
        value = 2 * rows[stop]["semi_diameter"] if ap["type"] == "float_by_stop" else ap["value"]
        if ap["type"] == "object_cone_angle":
            n = float(array(optic.surfaces[0].material_post.n(p["wavelengths"]["um"][p["wavelengths"]["primary"] - 1])).item())
            value = n * math.sin(math.radians(value))
        optic.set_aperture(APERTURES[ap["type"]], value)
        optic.set_field_type(p["fields"]["type"])
        for x, y, weight in zip(p["fields"]["x"], p["fields"]["y"], p["fields"]["weights"]):
            optic.fields.add(x=x, y=y, weight=weight)
        optic.obj_space_telecentric = p.get("telecentric_object", False)
        for i, (um, weight) in enumerate(zip(p["wavelengths"]["um"], p["wavelengths"]["weights"]), 1):
            optic.wavelengths.add(um, is_primary=i == p["wavelengths"]["primary"], weight=weight)
        optic.set_ray_aiming(self._aiming, max_iter=50 if self._aiming == "robust" else 20, tol=1e-10)
        if p.get("apodization"):
            spec = p["apodization"]
            if spec["type"] != "uniform":
                optic.set_apodization(spec["type"], value=spec.get("factor", 0))
        return optic

    @contextmanager
    def _using(self, optic, rows, p):
        previous = self.optic, self._rows, self._effective
        self.optic, self._rows, self._effective = optic, rows, p
        try:
            yield
        finally:
            self.optic, self._rows, self._effective = previous

    def _settle_solves(self, p, rows):
        done, visiting = set(), set()

        def read(i, key):
            return parameters(rows[i]).get(key, 0.0) if is_par(key) else rows[i].get(key, "" if key == "material" else math.inf if key == "radius" else 0.0)

        def write(i, key, value):
            (rows[i].setdefault("params", {}) if is_par(key) else rows[i])[key] = value

        def settle(i, key):
            node = (i, key)
            if node in done:
                return read(i, key)
            if node in visiting:
                raise SaosError(ErrorCode.BAD_ARGS, f"cyclic solve dependency at {i}.{key}")
            visiting.add(node)
            spec = rows[i].get("solves", {}).get(key, {})
            solve = spec.get("type")
            if solve == "pickup":
                source = spec["surface"]
                if not 0 <= source < len(rows) or source == i:
                    raise SaosError(ErrorCode.BAD_ARGS, "invalid pickup source surface")
                value = settle(source, key)
                if key != "material":
                    value = (0.0 if spec.get("scale", 1.0) == 0 else value * spec.get("scale", 1.0)) + spec.get("offset", 0.0)
                write(i, key, value)
            elif solve in ("f_number", "marginal_ray_height"):
                for j in range(i):
                    for column in rows[j].get("solves", {}):
                        settle(j, column)
                optic = self._construct(p, rows)
                if solve == "f_number":
                    from optiland.solves.curvature import MarginalRayAngleCurvatureSolve

                    if spec["value"] == 0:
                        raise SaosError(ErrorCode.BAD_ARGS, "f_number solve cannot use zero F/#")
                    target = -1 / (2 * spec["value"])
                    MarginalRayAngleCurvatureSolve(optic, i, target).apply()
                    _, angles = optic.paraxial.marginal_ray()
                    if not math.isclose(float(array(angles[i]).item()), target, abs_tol=1e-9, rel_tol=1e-7):
                        raise SaosError(ErrorCode.VALUE_NOT_APPLIED, f"F-number solve failed on surface {i}")
                    write(i, key, float(array(optic.surfaces[i].geometry.radius).item()))
                else:
                    from scipy.optimize import root_scalar

                    if i + 1 >= len(rows):
                        raise SaosError(ErrorCode.BAD_ARGS, "a marginal height solve needs a following surface")
                    start = read(i, key)

                    def residual(value):
                        write(i, key, value)
                        candidate = self._construct(p, rows)
                        if spec.get("zone", 0) == 0:
                            y, _ = candidate.paraxial.marginal_ray()
                            height = float(array(y).ravel()[i + 1])
                        else:
                            with self._using(candidate, rows, p):
                                hit = self._trace(np.array([[p["wavelengths"]["primary"], 0, 0, 0, spec["zone"]]], dtype=float), i + 1)
                            height = float(hit[0, 3]) if hit[0, 0] == 0 and hit[0, 1] == 0 else math.nan
                        if not math.isfinite(height):
                            raise SaosError(ErrorCode.VALUE_NOT_APPLIED, "marginal-height solve ray cannot be traced")
                        return height - spec["height"]

                    solution = root_scalar(residual, x0=start, x1=start + max(1e-5, abs(start) * 1e-4), method="secant", xtol=1e-11, maxiter=50)
                    if not solution.converged or abs(residual(solution.root)) > 1e-8:
                        raise SaosError(ErrorCode.VALUE_NOT_APPLIED, f"marginal-height solve failed on surface {i}")
                    write(i, key, solution.root)
            visiting.remove(node)
            done.add(node)
            return read(i, key)

        for i, row in enumerate(rows):
            for key in row.get("solves", {}):
                settle(i, key)

    def _sync_optical_state(self):
        key = self._revision, self._configuration, self._aiming
        if self._synchronized == key:
            return
        p = self._configuration_prescription()
        rows = copy.deepcopy([{"type": "standard", "radius": math.inf, **p["object"]}, *p["surfaces"],
                              {"type": "standard", "radius": math.inf, **p.get("image", {})}])
        scales = self._expand_thermal(p, rows)
        self._settle_solves(p, rows)
        candidate = self._construct(p, rows)
        previous = self.optic, self._rows, self._effective
        self.optic, self._rows, self._effective = candidate, rows, p
        try:
            self._update_apertures()
        except BaseException:
            self.optic, self._rows, self._effective = previous
            raise
        self._scales = scales
        self._synchronized = key

    @timing.timed("optiland.build")
    def build(self, prescription):
        capability = self.can_handle(prescription)
        if not capability["supported"]:
            raise unsupported("; ".join(capability["reasons"]))
        previous = self.__dict__.copy()
        self._prescription = copy.deepcopy(prescription)
        self._configuration = 1
        self._aiming = self.aiming_mode(prescription)
        self._bounds, self._merit, self._merit_script, self._glass_vary = {}, [], None, []
        self._invalidate()
        try:
            self._sync_optical_state()
        except BaseException:
            self.__dict__.update(previous)
            raise
        self.file = None

    # ---- 优化内循环：就地改活的 Optic ----

    def _live_capable(self, keys):
        """变量和求解链能否在活的 Optic 上就地更新；不能时优化每步经处方重建（慢，但结果相同）。"""
        def settable(i, column):
            row = self._rows[i]
            if column == "thickness":
                return True
            if column in ("radius", "conic"):
                return kind(row) in ("standard", "evenaspheric", "oddaspheric")
            if is_par(column) and kind(row) in ("evenaspheric", "oddaspheric"):
                return int(column[3:]) <= len(self.optic.surfaces[i].geometry.coefficients)
            return False

        if not all(settable(i, column) for i, column in keys):
            return False
        # 多重结构改写了变量所在的格时，按处方重建才能让结构值覆盖变量（同 OpticStudio）
        columns = {"THIC": "thickness", "CRVT": "radius", "CONI": "conic"}
        overridden = {(spec["params"][0], columns.get(spec["operand"]) or f"par{spec['params'][1]}")
                      for spec in self._require().get("configurations", []) if spec["operand"] in (*columns, "PRAM")}
        if overridden & set(keys):
            return False
        return all(spec["type"] != "pickup" or column == "material" or settable(i, column)
                   for i, row in enumerate(self._rows) for column, spec in row.get("solves", {}).items())

    def _live_write(self, i, column, value):
        """在活的 Optic 和逐面数据上写入一个有效值（已乘热膨胀系数）。厚度变化时其后各面沿该面的厚度方向整体平移。"""
        row, surface = self._rows[i], self.optic.surfaces[i]
        if column == "radius":
            value = math.inf if value == 0 else value
            self.optic.updater.set_radius(value, i)
        elif column == "conic":
            surface.geometry.k = value
        elif column == "thickness":
            if i == 0:
                self.optic.updater.set_thickness(value, 0)
            else:
                delta = value - float(row.get("thickness", 0.0))
                axis = self.optic.saos_axes[i]
                moved = set()
                for later in self.optic.surfaces[i + 1:]:
                    cs = later.geometry.cs
                    if id(cs) in moved:
                        continue
                    moved.add(id(cs))
                    cs.x, cs.y, cs.z = (float(np.asarray(v).ravel()[0]) + delta * a for v, a in zip((cs.x, cs.y, cs.z), axis))
                surface.thickness = value
        else:
            surface.geometry.coefficients[int(column[3:]) - 1] = value
            (row if column in row else row.setdefault("params", {}))[column] = value
            return
        row[column] = value

    def _live_set(self, i, column, nominal):
        scale = self._scales.get((i, column), 1.0)
        self._live_write(i, column, nominal if math.isinf(nominal) else nominal * scale)

    def _live_settle(self):
        """就地重算拾取、F 数和边缘光线高度求解（同 _settle_solves 的依赖顺序），只动活的 Optic。"""
        rows = self._rows
        if not any(spec["type"] in ("pickup", "f_number", "marginal_ray_height") and column != "material"
                   for row in rows for column, spec in row.get("solves", {}).items()):
            return
        done, visiting = set(), set()

        def read(i, key):
            return parameters(rows[i]).get(key, 0.0) if is_par(key) else rows[i].get(key, math.inf if key == "radius" else 0.0)

        def settle(i, key):
            node = (i, key)
            if node in done:
                return
            if node in visiting:
                raise SaosError(ErrorCode.BAD_ARGS, f"cyclic solve dependency at {i}.{key}")
            visiting.add(node)
            spec = rows[i].get("solves", {}).get(key, {})
            solve = spec.get("type")
            if solve == "pickup" and key != "material":
                settle(spec["surface"], key)
                scale = spec.get("scale", 1.0)
                self._live_write(i, key, (0.0 if scale == 0 else read(spec["surface"], key) * scale) + spec.get("offset", 0.0))
            elif solve in ("f_number", "marginal_ray_height"):
                for j in range(i):
                    for column in rows[j].get("solves", {}):
                        settle(j, column)
                if solve == "f_number":
                    from optiland.solves.curvature import MarginalRayAngleCurvatureSolve

                    MarginalRayAngleCurvatureSolve(self.optic, i, -1 / (2 * spec["value"])).apply()
                    rows[i]["radius"] = float(array(self.optic.surfaces[i].geometry.radius).item())
                else:
                    from scipy.optimize import root_scalar

                    primary = self._effective["wavelengths"]["primary"]

                    def residual(value):
                        self._live_write(i, "thickness", value)
                        if spec.get("zone", 0) == 0:
                            y, _ = self.optic.paraxial.marginal_ray()
                            height = float(array(y).ravel()[i + 1])
                        else:
                            hit = self._trace(np.array([[primary, 0, 0, 0, spec["zone"]]], dtype=float), i + 1)
                            height = float(hit[0, 3]) if hit[0, 0] == 0 and hit[0, 1] == 0 else math.nan
                        if not math.isfinite(height):
                            raise SaosError(ErrorCode.VALUE_NOT_APPLIED, "marginal-height solve ray cannot be traced")
                        return height - spec["height"]

                    start = read(i, "thickness")
                    solution = root_scalar(residual, x0=start, x1=start + max(1e-5, abs(start) * 1e-4), method="secant", xtol=1e-11, maxiter=50)
                    if not solution.converged or abs(residual(solution.root)) > 1e-8:
                        raise SaosError(ErrorCode.VALUE_NOT_APPLIED, f"marginal-height solve failed on surface {i}")
            visiting.remove(node)
            done.add(node)

        for i, row in enumerate(rows):
            for key in row.get("solves", {}):
                settle(i, key)

    def _nominal(self, i, column):
        """处方里的名义值（有效值除以热膨胀系数）。"""
        value = float(self.surface_value(i, column))
        return value if math.isinf(value) else value / self._scales.get((i, column), 1.0)

    def _commit(self, values):
        """把 {(面, 列): 名义值} 写回处方并完整同步（求解链、自动半口径）。"""
        last = len(self._rows) - 1

        def change(p):
            for (i, column), value in values.items():
                nominal = p["object"] if i == 0 else p.setdefault("image", {"type": "standard"}) if i == last else p["surfaces"][i - 1]
                if column in ("nd", "vd"):
                    nominal["material"] = {"model": {**nominal["material"]["model"], column: value}}
                elif kind(nominal) == "paraxial" and column in ("par1", "par2"):
                    nominal["focal_length" if column == "par1" else "opd_mode"] = value
                elif is_par(column) and column not in nominal:
                    nominal.setdefault("params", {})[column] = value
                else:
                    nominal[column] = value

        self._change(change)

    def vary(self, surface, param, on, mode=None, catalog=None):
        self._require()
        if param == "material":
            return self._vary_model_glass(surface, on, mode)
        column = "radius" if param == "curvature" else param
        if column not in ("radius", "thickness", "conic") and not is_par(column):
            raise SaosError(ErrorCode.BAD_ARGS, "only radius, curvature, thickness, conic, par<N> and material can be variables")
        last = len(self._rows) - 1
        if not (0 < surface < last or (surface == 0 and column == "thickness")):
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {surface} {param} cannot be a variable")
        if surface == 0 and math.isinf(float(self.surface_value(0, "thickness"))):
            raise SaosError(ErrorCode.BAD_ARGS, "the object is at infinity; its distance cannot be a variable")
        row = self._rows[surface]
        if column in ("radius", "conic") and kind(row) in ("coordinatebreak", "paraxial"):
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {surface} is a {DISPLAY[kind(row)]} surface without a {column}")
        current = row.get("solves", {}).get(column, {}).get("type")
        if on and current in ("pickup", "f_number", "marginal_ray_height"):
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {surface}.{column} is controlled by a {current} solve")
        if on == (current == "variable"):
            return "Variable" if on else "Fixed"
        explicit = is_par(column) and kind(row) in ("evenaspheric", "oddaspheric")

        def change(p):
            nominal = p["object"] if surface == 0 else p["surfaces"][surface - 1]
            solves = nominal.setdefault("solves", {})
            if on:
                solves[column] = {"type": "variable"}
                # 非球面系数补成显式参数，Optic 上才有这一项，内循环可以就地改它
                if explicit:
                    for n in range(1, int(column[3:]) + 1):
                        if f"par{n}" not in parameters(nominal):
                            nominal.setdefault("params", {})[f"par{n}"] = 0.0
            else:
                del solves[column]
                if not solves:
                    del nominal["solves"]

        self._change(change)
        if not on:
            self._bounds.pop(f"{surface}.{column}", None)
        return "Variable" if on else "Fixed"

    def _vary_model_glass(self, surface, on, mode):
        """材料变量：--mode model 把材料换成模型玻璃，nd、Vd 由优化器改（Optiland 阶段为 Abbe 近似）；
        --mode substitute 是 Hammer / 全局搜索里的牌号替换，交给 OpticStudio（Optiland 阶段换牌号用 opt glass）。"""
        if not on:
            # 同 OpticStudio：模型玻璃保留当前 nd、Vd，只是不再变化
            if surface in self._glass_vary:
                self._glass_vary.remove(surface)
            return "MaterialModel" if isinstance(self._rows[surface].get("material"), dict) else "Fixed"
        if mode == "substitute":
            raise unsupported("glass substitution by Hammer or global search runs in OpticStudio; in Optiland use saos opt glass")
        if mode != "model":
            raise SaosError(ErrorCode.BAD_ARGS, "material needs --mode model or substitute")
        if not 0 < surface < len(self._rows) - 1:
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {surface} material cannot be a variable")
        if not isinstance(self._rows[surface].get("material"), dict):
            self._set_model_glass(surface, "nd", self.surface_value(surface, "nd"))
        if surface not in self._glass_vary:
            self._glass_vary.append(surface)
        return "MaterialModel"

    def set_bounds(self, surface, param, minimum, maximum):
        """变量的物理上下界（半径以 mm、曲率以 1/mm 计），由优化器在边界内约束；两者都为 None 时去掉边界。"""
        self._require()
        column = "radius" if param == "curvature" else param
        if not 0 <= surface < len(self._rows) or self._rows[surface].get("solves", {}).get(column, {}).get("type") != "variable":
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {surface} {column} is not a variable")
        key = f"{surface}.{column}"
        if minimum is None and maximum is None:
            self._bounds.pop(key, None)
            return None
        for value in (minimum, maximum):
            if value is not None and not math.isfinite(value):
                raise SaosError(ErrorCode.BAD_ARGS, "--min and --max must be finite")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise SaosError(ErrorCode.BAD_ARGS, f"--min {minimum} is above --max {maximum}")
        if param == "radius" and (minimum is None or maximum is None or minimum * maximum <= 0):
            raise SaosError(ErrorCode.BAD_ARGS, "radius bounds need --min and --max of the same sign; bound --param curvature for a range "
                                                "that passes through a flat surface")
        self._bounds[key] = {"param": param, "min": minimum, "max": maximum}
        return dict(self._bounds[key])

    @staticmethod
    def aiming_mode(prescription):
        """OpticStudio 的 off 不瞄准，对应 Optiland paraxial；paraxial、real 都瞄准光阑，对应 robust。
        iterative 在离轴视场常整组不收敛（双高斯 20° 视场一组 149 根全部失败），robust 的落点与 OpticStudio 相差 < 5e-10 mm。"""
        return "paraxial" if prescription.get("ray_aiming") == "off" else "robust"

    def configuration_count(self):
        rows = self._require().get("configurations", [])
        return len(rows[0]["values"]) if rows else 1

    def current_configuration(self):
        return self._configuration

    def select_configuration(self, n):
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= self.configuration_count():
            raise SaosError(ErrorCode.BAD_ARGS, f"configuration must be 1–{self.configuration_count()}")
        before = self._configuration
        self._configuration = n
        try:
            self._sync_optical_state()
        except BaseException:
            self._configuration = before
            raise

    def _normalized_fields(self):
        p = self._effective or self._require()
        fields = np.column_stack([p["fields"]["x"], p["fields"]["y"]])
        if p["fields"].get("normalization", "radial") == "rectangular":
            extent = np.max(np.abs(fields), axis=0)
            return (fields / np.where(extent > 0, extent, 1.0)).tolist()
        extent = float(np.max(np.linalg.norm(fields, axis=1)))
        return (fields / extent if extent else fields).tolist()

    def _native_fields(self, hx, hy):
        p = self._effective
        fields = np.column_stack([p["fields"]["x"], p["fields"]["y"]])
        if p["fields"].get("normalization", "radial") != "rectangular":
            return hx, hy
        radial = float(np.max(np.linalg.norm(fields, axis=1)))
        extent = np.max(np.abs(fields), axis=0)
        return hx * extent[0] / radial if radial else hx * 0, hy * extent[1] / radial if radial else hy * 0

    def surface_value(self, index, param):
        self._require()
        if not 0 <= index < len(self._rows):
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {index} out of range")
        row = self._rows[index]
        if param == "mech_semi_diameter":
            return row.get(param, row.get("semi_diameter", 0.0))
        if param in ("nd", "vd"):
            material = self._store.get(row.get("material", ""), self._effective["catalogs"])
            metadata = getattr(material, "metadata", {})
            if param in metadata:
                return metadata[param]
            nd = float(array(material.n(0.5875618)).item())
            difference = float(array(material.n(0.4861327) - material.n(0.6562725)).item()) if param == "vd" else 0
            return nd if param == "nd" else (nd - 1) / difference if difference else math.inf
        if is_par(param):
            if kind(row) == "paraxial":
                return row.get("focal_length") if param == "par1" else row.get("opd_mode", 1) if param == "par2" else 0.0
            return parameters(row).get(param, 0.0)
        if param == "material" and isinstance(row.get("material"), dict):
            model = row["material"]["model"]
            return f"MODEL {model['nd']:.6g},{model['vd']:.6g}"
        return row.get(param, "" if param in ("material", "comment") else math.inf if param == "radius" else 0.0)

    def get_param(self, key):
        from types import SimpleNamespace

        from saos.kit import parse_key, surface

        category, ref, param = parse_key(key)
        if category == "surface":
            i = surface(SimpleNamespace(backend=self), ref)
            value = self.surface_value(i, "radius" if param == "curvature" else param)
            return (0.0 if value == 0 or math.isinf(value) else 1 / value) if param == "curvature" else value
        if category != "system":
            raise unsupported(f"parameter {key} requires ZOS")
        p = self._effective
        if ref == "aperture":
            return p["aperture"]["value"]
        if ref == "config":
            c, row = map(int, param.split("."))
            if not 1 <= c <= self.configuration_count() or not 1 <= row <= len(self._prescription.get("configurations", [])):
                raise SaosError(ErrorCode.BAD_ARGS, f"configuration cell {key} out of range")
            return self._prescription["configurations"][row - 1]["values"][c - 1]
        sequence = p["wavelengths"]["um"] if ref == "wave" else p["fields"][ref[-1]]
        if not 1 <= int(param) <= len(sequence):
            raise SaosError(ErrorCode.BAD_ARGS, f"parameter index out of range: {key}")
        return sequence[int(param) - 1]

    def _change(self, change):
        previous = self._prescription
        candidate = copy.deepcopy(previous)
        change(candidate)
        self._prescription = candidate
        self._invalidate()
        try:
            self._sync_optical_state()
        except BaseException:
            self._prescription = previous
            self._synchronized = None
            raise

    def set_param(self, key, value):
        from types import SimpleNamespace

        from saos.kit import parse_key, surface

        category, ref, param = parse_key(key)
        if category == "surface":
            self.set_surface(surface(SimpleNamespace(backend=self), ref), param, value)
            return
        self.get_param(key)
        if ref == "config":
            c, row = map(int, param.split("."))
            spec = self._prescription["configurations"][row - 1]
            target = str(value) if spec["operand"] == "GLSS" else parse_numeric(key, value)
            self._change(lambda p: p["configurations"][row - 1]["values"].__setitem__(c - 1, target))
            return
        number = parse_numeric(key, value)
        if not math.isfinite(number) or (ref in ("aperture", "wave") and number <= 0):
            raise SaosError(ErrorCode.BAD_ARGS, f"invalid {key} value")
        if ref == "aperture":
            if self._prescription["aperture"]["type"] == "float_by_stop":
                raise SaosError(ErrorCode.BAD_ARGS, "set the stop semi_diameter for a floating aperture")
            self._change(lambda p: p["aperture"].__setitem__("value", number))
        elif ref == "wave":
            self._change(lambda p: p["wavelengths"]["um"].__setitem__(int(param) - 1, number))
        else:
            if self._prescription["fields"]["type"] == "angle" and abs(number) >= 90:
                raise SaosError(ErrorCode.BAD_ARGS, "angle fields must stay below 90 degrees")
            self._change(lambda p: p["fields"][ref[-1]].__setitem__(int(param) - 1, number))

    def set_surface(self, index, param, value):
        from saos.kit import PARAMS

        if param not in PARAMS and not is_par(param):
            raise SaosError(ErrorCode.BAD_ARGS, f"unsupported surface parameter {param}")
        if param in ("nd", "vd"):
            return self._set_model_glass(index, param, value)
        before = self.surface_value(index, "radius" if param == "curvature" else param)
        target = str(value or "") if param in ("material", "comment") else parse_numeric(param, value)
        if param == "curvature":
            param, target = "radius", math.inf if target == 0 else 1 / target
        if param not in ("material", "comment"):
            check_size(param, target)
            if math.isnan(target) or (not math.isfinite(target) and param not in ("radius", "thickness")):
                raise SaosError(ErrorCode.BAD_ARGS, f"{param} requires a finite value")
        if target == before:
            return self.surfaces()[index]
        row = self._rows[index]
        if row.get("solves", {}).get(param, {}).get("type") in ("pickup", "f_number", "marginal_ray_height"):
            raise SaosError(ErrorCode.VALUE_NOT_APPLIED, f"surface {index}.{param} is controlled by a solve")

        def change(p):
            nominal = p["object"] if index == 0 else p.setdefault("image", {"type": "standard"}) if index == len(self._rows) - 1 else p["surfaces"][index - 1]
            value = target if isinstance(target, str) else target / self._scales.get((index, param), 1.0)
            if kind(nominal) == "paraxial" and param in ("par1", "par2"):
                nominal["focal_length" if param == "par1" else "opd_mode"] = value
            else:
                (nominal.setdefault("params", {}) if is_par(param) else nominal)[param] = value

        self._change(change)
        actual = self.surface_value(index, param)
        if actual != target and not (isinstance(actual, (float, int)) and isinstance(target, (float, int)) and same_number(actual, target)):
            raise SaosError(ErrorCode.VALUE_NOT_APPLIED, f"surface {index}.{param} was overridden", {"requested": target, "actual": actual})
        return self.surfaces()[index]

    def _set_model_glass(self, index, param, value):
        """改 nd 或 vd：材料换成模型玻璃（另一项取当前值，ΔPgF 为 0），Optiland 阶段用 Abbe 材料近似。"""
        target = parse_numeric(param, value)
        if not math.isfinite(target) or target <= (1.0 if param == "nd" else 0.0):
            raise SaosError(ErrorCode.BAD_ARGS, f"{param} must be a finite number above {1 if param == 'nd' else 0}")
        material = self._rows[index].get("material", "")
        if not material or (isinstance(material, str) and material.upper() in ("AIR", "MIRROR")):
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {index} has no glass ({material or 'air'}) to give an {param}")
        model = {"nd": float(self.surface_value(index, "nd")), "vd": float(self.surface_value(index, "vd")), "dpgf": 0.0}
        model[param] = target
        self._change(lambda p: self._nominal_row(p, index).__setitem__("material", {"model": model}))
        return self.surfaces()[index]

    def _nominal_row(self, p, index):
        last = len(self._rows) - 1
        return p["object"] if index == 0 else p.setdefault("image", {"type": "standard"}) if index == last else p["surfaces"][index - 1]

    def surfaces(self):
        self._require()
        return [{"index": i, "type": DISPLAY[kind(row)], "is_stop": self.optic.surfaces[i].is_stop,
                 **{k: self.surface_value(i, k) for k in ("radius", "thickness", "material", "comment", "conic", "semi_diameter", "mech_semi_diameter")},
                 "aperture": aperture_description(row.get("aperture")),
                 "solves": {k: spec["type"] for k, spec in row.get("solves", {}).items()}}
                for i, row in enumerate(self._rows)]

    def primary_wave(self):
        return self._effective["wavelengths"]["primary"]

    def wavelength_count(self):
        return len(self._effective["wavelengths"]["um"])

    def append_field(self, x, y, weight=1.0):
        def change(p):
            for key, value in (("x", x), ("y", y), ("weights", weight)):
                p["fields"][key].append(float(value))
        self._change(change)
        return len(self._effective["fields"]["x"])

    def truncate_fields(self, count):
        def change(p):
            for key in ("x", "y", "weights"):
                del p["fields"][key][count:]
        self._change(change)

    def system_info(self):
        from saos.prescription import APERTURES as AP_NAMES
        from saos.prescription import FIELDS

        p = self._effective
        self._require()
        return {"backend": "optiland", "version": config.OPTILAND_VERSION, "file": str(self.file or ""), "title": p.get("title", ""),
                "lens_units": "Millimeters", "num_surfaces": len(self._rows), "stop_surface": int(self.optic.surfaces.stop_index),
                "aperture": {"type": AP_NAMES[p["aperture"]["type"]], "value": p["aperture"]["value"],
                             "telecentric_object": p.get("telecentric_object", False), "afocal_image": False},
                "fields": {"type": FIELDS[p["fields"]["type"]], "normalization": p["fields"].get("normalization", "radial").capitalize(),
                           "items": [dict(x=x, y=y, weight=w) for x, y, w in zip(p["fields"]["x"], p["fields"]["y"], p["fields"]["weights"])]},
                "wavelengths": [dict(um=um, weight=w, primary=i == p["wavelengths"]["primary"]) for i, (um, w) in
                                enumerate(zip(p["wavelengths"]["um"], p["wavelengths"]["weights"]), 1)],
                "material_catalogs": list(p["catalogs"]), "configuration": self._configuration, "ray_aiming": self._aiming}

    def snapshot(self):
        output = copy.deepcopy(self._rows)
        for i, row in enumerate(output):
            row["index"] = i
        return output
