"""Optiland 后端的操作数表。参数位置与 OpticStudio 的 GetOperandValue 相同：int1、int2、data（Hx, Hy, Px, Py, …）。

只收录手册定义明确、能在 Optiland 上按同一定义计算的操作数；其余报 BACKEND_UNSUPPORTED，不给近似值。
"""

from __future__ import annotations

import copy
import math

import numpy as np

from saos.backend.optiland_analysis import opd_samples, seidel_terms, zernike
from saos.backend.optiland_materials import unsupported
from saos.backend.optiland_state import array
from saos.protocol import ErrorCode, SaosError

SEIDEL = {"SPHA": (0, 8.0), "COMA": (1, 2.0), "ASTI": (2, 2.0), "DIST": (4, 2.0)}
GQ_ARMS = 12


def _scalar(value) -> float:
    return float(np.asarray(array(value)).ravel()[0])


def _wave(b, wave: int) -> int:
    wave = wave or b.primary_wave()
    if not 1 <= wave <= b.wavelength_count():
        raise SaosError(ErrorCode.BAD_ARGS, f"wavelength {wave} is out of range")
    return wave


def _surface(b, index: int) -> int:
    if not 0 <= index < len(b._rows):
        raise SaosError(ErrorCode.BAD_ARGS, f"surface {index} is out of range 0–{len(b._rows) - 1}")
    return index


def _at_wave(b, wave: int, compute):
    before = b.optic.wavelengths.primary_index
    try:
        b.optic.wavelengths.primary_index = wave - 1
        return compute()
    finally:
        b.optic.wavelengths.primary_index = before


def _ray(data):
    return [float(v) for v in (list(data) + [0.0] * 4)[:4]]


def trace_record(b, wave: int, hx: float, hy: float, px: float, py: float, upto: int) -> list[dict]:
    """一条实际光线逐面的记录：局部坐标、出射方向（局部）、累计光程、入射角（度）。光线失败后的面不再记录。"""
    be = b._configure()
    nhx, nhy = b._native_fields(hx, hy)
    um = b._effective["wavelengths"]["um"][wave - 1]
    try:
        rays = b.optic.ray_tracer.ray_generator.generate_rays(float(nhx), float(nhy), be.array([px]), be.array([py]), um)
    except (ValueError, ZeroDivisionError, FloatingPointError) as exc:
        raise SaosError(ErrorCode.OPTILAND_ERROR, f"ray aiming failed for ({hx}, {hy}, {px}, {py}): {exc}") from exc
    records = []
    for i, optical_surface in enumerate(b.optic.surfaces):
        if i > upto:
            break
        incident = np.array([_scalar(rays.L), _scalar(rays.M), _scalar(rays.N)])
        with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
            optical_surface.trace(rays, record=False)
        if i == 0:
            # 物面在无穷远时局部坐标是无穷大，不能当作光线失败 / an object at infinity has infinite local coordinates
            records.append({"position": np.full(3, math.nan), "direction": np.array([_scalar(rays.L), _scalar(rays.M), _scalar(rays.N)]),
                            "opd": _scalar(rays.opd), "intensity": _scalar(rays.i), "aoi": math.nan})
            continue
        local = copy.deepcopy(rays)
        optical_surface.geometry.cs.localize(local)
        position = np.array([_scalar(local.x), _scalar(local.y), _scalar(local.z)])
        direction = np.array([_scalar(local.L), _scalar(local.M), _scalar(local.N)])
        if not np.isfinite(position).all() or not np.isfinite(direction).all():
            break
        _, rotation = optical_surface.geometry.cs.get_effective_transform()
        incident_local = np.asarray(array(rotation)).reshape(3, 3).T @ incident
        with np.errstate(invalid="ignore", divide="ignore"):
            normal = np.array([_scalar(v) for v in optical_surface.geometry.surface_normal(local)])
        cosine = abs(float(np.dot(incident_local, normal)) / (np.linalg.norm(normal) or math.nan))
        records.append({"position": position, "direction": direction, "opd": _scalar(rays.opd), "intensity": _scalar(rays.i),
                        "aoi": math.degrees(math.acos(min(1.0, cosine))) if math.isfinite(cosine) else math.nan})
    return records


def _record(b, surface: int, wave: int, data) -> dict | None:
    hx, hy, px, py = _ray(data)
    records = trace_record(b, wave, hx, hy, px, py, surface)
    return records[surface] if len(records) > surface else None


def _paraxial_point(b, surface: int, wave: int, data) -> tuple[float, float]:
    """近轴光线在面上的 (x, y)：旋转对称系统中 = H·主光线高度 + P·边缘光线高度。"""
    hx, hy, px, py = _ray(data)

    def rays():
        ym, _ = b.optic.paraxial.marginal_ray()
        yc, _ = b.optic.paraxial.chief_ray()
        return np.asarray(array(ym)).ravel(), np.asarray(array(yc)).ravel()

    ym, yc = _at_wave(b, wave, rays)
    return hx * yc[surface] + px * ym[surface], hy * yc[surface] + py * ym[surface]


def _efl_range(b, first: int, last: int) -> float:
    if not 1 <= first <= last < len(b._rows):
        raise SaosError(ErrorCode.BAD_ARGS, f"surface range {first}–{last} is invalid")
    matrix = np.asarray(array(b.optic.paraxial.ray_transfer_matrix(first, last)))
    c = float(matrix[1, 0])
    return math.inf if c == 0 else -1.0 / c


def _marginal_image(b, wave: int) -> dict:
    image = len(b._rows) - 1
    rec = _record(b, image, wave, [0.0, 0.0, 0.0, 1.0])
    if rec is None or rec["intensity"] <= 0:
        raise SaosError(ErrorCode.OPTILAND_ERROR, "the real marginal ray does not reach the image")
    return rec


def _gq_pupil(rings: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if not 1 <= rings <= 20:
        raise SaosError(ErrorCode.BAD_ARGS, "Gaussian quadrature needs 1–20 rings")
    nodes, weights = np.polynomial.legendre.leggauss(rings)
    rho = np.sqrt((nodes + 1) / 2)
    angles = 2 * np.pi * (np.arange(GQ_ARMS) + 0.5) / GQ_ARMS
    px = np.outer(rho, np.cos(angles)).ravel()
    py = np.outer(rho, np.sin(angles)).ravel()
    w = np.repeat(weights / 2 / GQ_ARMS, GQ_ARMS)
    return px, py, w


def _rms_spot(b, name: str, rings: int, wave: int, data) -> float:
    hx, hy = _ray(data)[:2]
    px, py, weights = _gq_pupil(rings)
    waves = [wave] if wave else list(range(1, b.wavelength_count() + 1))
    wave_weights = b._effective["wavelengths"]["weights"]
    image = len(b._rows) - 1
    xs, ys, ws = [], [], []
    for w in waves:
        traced = b.trace_batch([(w, hx, hy, x, y) for x, y in zip(px, py)], image)
        if not traced.ok.all():
            raise SaosError(ErrorCode.OPTILAND_ERROR, f"{name}: Gaussian quadrature needs an unvignetted pupil; rays failed or were clipped")
        xs.append(traced.x), ys.append(traced.y), ws.append(weights * (wave_weights[w - 1] if not wave else 1.0))
    x, y, w = np.concatenate(xs), np.concatenate(ys), np.concatenate(ws)
    if w.sum() <= 0:
        raise SaosError(ErrorCode.BAD_ARGS, f"{name}: the wavelength weights are zero")
    w = w / w.sum()
    if name == "RSCE":
        cx, cy = np.dot(w, x), np.dot(w, y)
    else:
        chief = b.trace_batch([(wave or b.primary_wave(), hx, hy, 0.0, 0.0)], image)
        if not chief.ok[0]:
            raise SaosError(ErrorCode.OPTILAND_ERROR, f"{name}: the chief ray fails")
        cx, cy = chief.x[0], chief.y[0]
    return float(np.sqrt(np.dot(w, (x - cx) ** 2 + (y - cy) ** 2)))


def _rms_wavefront(b, name: str, rings: int, wave: int, data) -> float:
    hx, hy = _ray(data)[:2]
    px, py, weights = _gq_pupil(rings)
    waves = [wave] if wave else list(range(1, b.wavelength_count() + 1))
    wave_weights = b._effective["wavelengths"]["weights"]
    total, norm = 0.0, 0.0
    for w in waves:
        x, y, opd = opd_samples(b, w, hx, hy, px, py)
        if len(opd) != len(px):
            raise SaosError(ErrorCode.OPTILAND_ERROR, f"{name}: Gaussian quadrature needs an unvignetted pupil")
        if name == "RWCE":
            design = np.column_stack([np.ones_like(x), x, y]) * np.sqrt(weights)[:, None]
            coef, *_ = np.linalg.lstsq(design, opd * np.sqrt(weights), rcond=None)
            opd = opd - np.column_stack([np.ones_like(x), x, y]) @ coef
        else:
            opd = opd - np.dot(weights, opd)
        weight = wave_weights[w - 1] if not wave else 1.0
        total += weight * float(np.dot(weights, opd ** 2))
        norm += weight
    if norm <= 0:
        raise SaosError(ErrorCode.BAD_ARGS, f"{name}: the wavelength weights are zero")
    return math.sqrt(total / norm)


def _edge_thickness(b, surface: int, code: int, mode: int) -> float:
    if code != 0 or mode not in (0, 1):
        raise unsupported("ETVA is mapped for the +y edge (Code 0) with Mode 0 (mech semi-diameter) or 1 (clear semi-diameter)")
    if not 1 <= surface < len(b._rows) - 1:
        raise SaosError(ErrorCode.BAD_ARGS, "ETVA needs a surface with a following surface")
    r = float(b.surface_value(surface, "semi_diameter" if mode else "mech_semi_diameter"))
    front, back = b.sag(surface, 0.0, r), b.sag(surface + 1, 0.0, r)
    if front is None or back is None:
        raise SaosError(ErrorCode.OPTILAND_ERROR, f"sag undefined at the edge of surface {surface} or {surface + 1}")
    return float(b.surface_value(surface, "thickness")) + back - front


def operand(b, name: str, int1: int, int2: int, data: list[float]) -> float:
    name = name.upper()
    if len(data) > 6:
        raise SaosError(ErrorCode.BAD_ARGS, "an operand takes at most 6 float parameters")
    if name == "EFFL":
        return _at_wave(b, _wave(b, int2), lambda: _scalar(b.optic.paraxial.f2()))
    if name in ("EFLX", "EFLY"):
        return _at_wave(b, b.primary_wave(), lambda: _efl_range(b, int1, int2))
    if name == "PMAG":
        return _at_wave(b, _wave(b, int2), lambda: _scalar(b.optic.paraxial.magnification()))
    if name == "EPDI":
        return _scalar(b.optic.paraxial.EPD())
    if name == "ENPP":
        return _scalar(b.optic.paraxial.EPL())
    if name == "EXPD":
        xpd = _scalar(b.optic.paraxial.XPD())
        if b._aiming == "paraxial":
            return xpd
        # 开光线瞄准时 OpticStudio 按实际光阑大小给出瞳径：近轴出瞳径 × 实际/近轴边缘光线在光阑上的高度
        # （2026-10-20 Cooke 三片镜：关瞄准两端差 1e-8；开瞄准时这样缩放后差 5e-6，不缩放差 1.5%）
        stop = int(b.optic.surfaces.stop_index)
        real = _record(b, stop, b.primary_wave(), [0.0, 0.0, 0.0, 1.0])
        ym, _ = b.optic.paraxial.marginal_ray()
        paraxial = float(np.asarray(array(ym)).ravel()[stop])
        if real is None or paraxial == 0:
            raise SaosError(ErrorCode.OPTILAND_ERROR, "EXPD: the real marginal ray does not reach the stop")
        return xpd * abs(float(real["position"][1]) / paraxial)
    if name == "EXPP":
        return _scalar(b.optic.paraxial.XPL())
    if name == "ISFN":
        return _scalar(b.optic.paraxial.f2()) / _scalar(b.optic.paraxial.EPD())
    if name == "WFNO":
        rec = _marginal_image(b, b.primary_wave())
        n = float(b.operand("INDX", len(b._rows) - 2, b.primary_wave(), []))
        sine = math.hypot(rec["direction"][0], rec["direction"][1])
        return math.inf if sine == 0 else 1 / (2 * n * sine)
    if name == "OBSN":
        if math.isinf(float(b.surface_value(0, "thickness"))):
            return 0.0
        rec = _record(b, 0, b.primary_wave(), [0.0, 0.0, 0.0, 1.0])
        if rec is None:
            raise SaosError(ErrorCode.OPTILAND_ERROR, "the object-space marginal ray cannot be traced")
        n = float(b.operand("INDX", 0, b.primary_wave(), []))
        return n * math.hypot(rec["direction"][0], rec["direction"][1])
    if name == "TOTR":
        return float(sum(float(b.surface_value(i, "thickness")) for i in range(1, len(b._rows) - 1)))
    if name in ("CTVA",):
        return float(b.surface_value(_surface(b, int1), "thickness"))
    if name == "ETVA":
        # saos 的显微物镜验收按 OpticStudio 列序传 (Surf, Code, 0, Mode)
        zone, mode = (list(data) + [0.0, 0.0])[:2]
        if zone:
            raise unsupported("ETVA is mapped with the third column 0 only")
        return _edge_thickness(b, _surface(b, int1), int2, int(mode))
    if name == "SSAG":
        # 列序同 OpticStudio：Surf、(未用)、X、Y、Mode；Mode 0 时净口径外按平的零件边缘（不计倒角区），Mode 1 按面方程
        surface = _surface(b, int1)
        x, y, mode = (list(data) + [0.0, 0.0, 0.0])[:3]
        if int(mode) not in (0, 1) or any(data[3:]):
            raise unsupported("SSAG is mapped for Mode 0 and 1 without off-axis, remove or best-fit-sphere options")
        r, edge = math.hypot(x, y), float(b.surface_value(surface, "semi_diameter"))
        if int(mode) == 0 and r > edge > 0:
            x, y = x * edge / r, y * edge / r
        value = b.sag(surface, x, y)
        return math.nan if value is None else value
    if name in ("SAGX", "SAGY"):
        surface = _surface(b, int1)
        r = float(b.surface_value(surface, "semi_diameter"))
        value = b.sag(surface, r if name == "SAGX" else 0.0, r if name == "SAGY" else 0.0)
        return math.nan if value is None else value
    if name == "INDX":
        surface, wave = _surface(b, int1), _wave(b, int2)
        return _scalar(b.optic.surfaces[surface].material_post.n(b._effective["wavelengths"]["um"][wave - 1]))
    if name in ("PARX", "PARY"):
        point = _paraxial_point(b, _surface(b, int1), _wave(b, int2), data)
        return point[0] if name == "PARX" else point[1]
    if name in ("REAX", "REAY", "REAZ", "REAA", "REAB", "REAC", "RANG", "RAID"):
        if int1 == 0 and name in ("REAX", "REAY", "REAZ", "RAID") and math.isinf(float(b.surface_value(0, "thickness"))):
            raise unsupported(f"{name} on an object at infinity follows OpticStudio's own convention, which has not been mapped")
        rec = _record(b, _surface(b, int1), _wave(b, int2), data)
        if rec is None:
            return math.nan
        if name in ("REAX", "REAY", "REAZ"):
            return float(rec["position"]["XYZ".index(name[-1])])
        if name in ("REAA", "REAB", "REAC"):
            return float(rec["direction"]["ABC".index(name[-1])])
        if name == "RANG":
            return math.acos(max(-1.0, min(1.0, float(rec["direction"][2]))))
        return rec["aoi"]
    if name == "PLEN":
        first, last = sorted((_surface(b, int1), _surface(b, int2)))
        hx, hy, px, py = _ray(data)
        records = trace_record(b, b.primary_wave(), hx, hy, px, py, last)
        if len(records) <= last:
            return math.nan
        return records[last]["opd"] - records[first]["opd"]
    if name in ("RSCE", "RSCH"):
        return _rms_spot(b, name, int1, int2 if int2 == 0 else _wave(b, int2), data)
    if name in ("RWCE", "RWCH"):
        return _rms_wavefront(b, name, int1, int2 if int2 == 0 else _wave(b, int2), data)
    if name in (*SEIDEL, "FCUR"):
        wave = _wave(b, int2)
        surface = int1
        if name == "DIST" and surface == 0:
            raise unsupported("DIST with Surf 0 (percent distortion) has not been mapped; use the FieldCurvatureAndDistortion analysis")
        s = seidel_terms(b, wave)
        if not 0 <= surface <= len(s):
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {surface} has no Seidel term")
        row = s.sum(axis=0) if surface == 0 else s[surface - 1]
        lam = b._effective["wavelengths"]["um"][wave - 1] * 1e-3
        if name == "FCUR":
            # OpticStudio: S4 = 4·W220 − 2·W222 且 S3 = 2·W222，故 W220 = (S3 + S4) / 4
            return float((row[2] + row[3]) / 4 / lam)
        column, divisor = SEIDEL[name]
        return float(row[column] / divisor / lam)
    if name == "ZERN":
        samp, field, kind, epsilon, vertex = (list(data) + [0.0] * 5)[:5]
        if int(kind) != 1 or epsilon or vertex:
            raise unsupported("ZERN is mapped for Zernike Standard (Type 1) referenced to the chief ray only")
        term = int1
        return zernike(b, wave=_wave(b, int2), field=int(field) or 1, samp=int(samp) or 1, max_term=max(term, 11), terms=(term,))[term]
    raise unsupported(f"operand {name} has not been mapped to Optiland; select SAOS_BACKEND=zos for it")
