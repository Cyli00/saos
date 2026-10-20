"""Optiland 后端的 Zernike 拟合与分析导出，输出结构与 backend/analysis.py 对 ZOS 结果的导出一致。

Zernike 按 OpticStudio Zernike Standard 的约定自行拟合（Noll 编号与归一化、均匀方格采样、参考主光线），
不用 optiland.zernike：后者的 standard 是 OSA/ANSI 编号，且在六角采样上拟合。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np

from saos.backend.optiland_materials import unsupported
from saos.backend.optiland_state import array
from saos.protocol import ErrorCode, SaosError

MAX_TERMS = 231
MIN_TERMS = 11
ANALYSES = ("StandardSpot", "FftMtf", "RayFan", "FieldCurvatureAndDistortion", "WavefrontMap", "SeidelCoefficients")
SAMPLE_SIZES = {"S_32x32": 32, "S_64x64": 64, "S_128x128": 128, "S_256x256": 256, "S_512x512": 512}


def noll_nm(j: int) -> tuple[int, int, str]:
    n = 0
    while (n + 1) * (n + 2) // 2 < j:
        n += 1
    k = j - n * (n + 1) // 2 - 1
    m = 2 * ((k + 1) // 2) if n % 2 == 0 else 2 * (k // 2) + 1
    return n, m, "" if m == 0 else ("cos" if j % 2 == 0 else "sin")


def noll_basis(j: int, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    n, m, part = noll_nm(j)
    rho, phi = np.hypot(x, y), np.arctan2(y, x)
    radial = np.zeros_like(rho)
    for s in range((n - m) // 2 + 1):
        coef = (-1) ** s * math.factorial(n - s) / (math.factorial(s) * math.factorial((n + m) // 2 - s) * math.factorial((n - m) // 2 - s))
        radial = radial + coef * rho ** (n - 2 * s)
    if m == 0:
        return math.sqrt(n + 1) * radial
    angular = np.cos(m * phi) if part == "cos" else np.sin(m * phi)
    return math.sqrt(2 * (n + 1)) * radial * angular


def _detrend(x, y, w):
    design = np.column_stack([np.ones_like(x), x, y])
    coef, *_ = np.linalg.lstsq(design, w, rcond=None)
    return w - design @ coef


def opd_samples(b, wave: int, hx: float, hy: float, px: np.ndarray, py: np.ndarray):
    """归一化光瞳坐标 (px, py) 上的 OPD（波），参考主光线球面；返回 (px, py, opd)，只留未失败、未渐晕的光线。"""
    from optiland.distribution import create_distribution
    from optiland.wavefront import Wavefront

    be = b._configure()
    distribution = create_distribution("uniform")
    distribution.x, distribution.y = be.array(px), be.array(py)
    native = b._native_fields(hx, hy)
    um = b._effective["wavelengths"]["um"][wave - 1]
    try:
        wavefront = Wavefront(b.optic, fields=[tuple(map(float, native))], wavelengths=[um], distribution=distribution,
                              strategy="chief_ray")
    except (ValueError, ZeroDivisionError, FloatingPointError) as exc:
        raise SaosError(ErrorCode.OPTILAND_ERROR, f"cannot compute the OPD at field ({hx}, {hy}), wave {wave}: {exc}") from exc
    data = wavefront.get_data(tuple(map(float, native)), um)
    opd = np.asarray(be.to_numpy(data.opd), dtype=float).ravel()
    keep = (np.asarray(be.to_numpy(data.intensity), dtype=float).ravel() > 0) & np.isfinite(opd)
    return px[keep], py[keep], opd[keep]


def pupil_grid(n: int) -> tuple[np.ndarray, np.ndarray]:
    axis = np.linspace(-1.0, 1.0, n)
    x, y = np.meshgrid(axis, axis)
    inside = x * x + y * y <= 1.0
    return x[inside], y[inside]


def zernike(b, *, wave: int, field: int, samp: int, max_term: int, terms: tuple[int, ...]) -> dict[int, float]:
    """一次拟合到 max_term 项（至少 11 项，同 ZERN），返回所需各项；负项号与 0 的含义同 ZERN：
    -8 相对质心 PV，-7 相对主光线 PV，-6 相对零点 RMS，-5 相对主光线 RMS，-4 相对质心 RMS，-3 方差，-2 Strehl 估计，
    -1 拟合 RMS 残差，0 拟合最大单点残差。统计量取自采样光线（OpticStudio 的 "from rays"）。"""
    for name, value in (("wave", wave), ("field", field), ("samp", samp), ("max_term", max_term)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise SaosError(ErrorCode.BAD_ARGS, f"zernike {name} must be an integer")
    if not 1 <= wave <= b.wavelength_count():
        raise SaosError(ErrorCode.BAD_ARGS, f"wavelength {wave} is out of range")
    fields = b._normalized_fields()
    if not 1 <= field <= len(fields):
        raise SaosError(ErrorCode.BAD_ARGS, f"field {field} is out of range")
    if not 1 <= samp <= 5:
        raise SaosError(ErrorCode.BAD_ARGS, "samp must be 1–5 (32×32 … 512×512)")
    count = max(MIN_TERMS, max_term, *[t for t in terms if t > 0])
    if count > MAX_TERMS or any(t < -8 for t in terms):
        raise SaosError(ErrorCode.BAD_ARGS, f"Zernike Standard terms go up to {MAX_TERMS}; negative terms down to -8")
    key = ("zernike", b._revision, b._configuration, b._aiming, wave, field, samp, count)
    if key not in b._analysis_cache:
        hx, hy = fields[field - 1]
        # OpticStudio 的 N×N 采样实测对应 N+1 个格点（含中心和 ±1）：Cooke 三片镜上 RMS、PV 与 ZERN 相差 ≤ 2e-4，用 N 个格点差 2.7%
        x, y, w = opd_samples(b, wave, hx, hy, *pupil_grid(32 * 2 ** (samp - 1) + 1))
        if len(w) <= count:
            raise SaosError(ErrorCode.OPTILAND_ERROR, f"only {len(w)} unvignetted rays for a {count}-term Zernike fit",
                            {"wave": wave, "field": field})
        design = np.column_stack([noll_basis(j, x, y) for j in range(1, count + 1)])
        coef, *_ = np.linalg.lstsq(design, w, rcond=None)
        residual = w - design @ coef
        centroid = _detrend(x, y, w)
        rms_centroid = float(np.sqrt(np.mean(centroid ** 2)))
        stats = {0: float(np.max(np.abs(residual))), -1: float(np.sqrt(np.mean(residual ** 2))),
                 -2: math.exp(-(2 * math.pi * rms_centroid) ** 2), -3: rms_centroid ** 2, -4: rms_centroid,
                 -5: float(np.std(w)), -6: float(np.sqrt(np.mean(w ** 2))), -7: float(np.ptp(w)), -8: float(np.ptp(centroid))}
        b._analysis_cache[key] = {**stats, **{j: float(c) for j, c in enumerate(coef, 1)}}
    fitted = b._analysis_cache[key]
    return {t: fitted[t] for t in terms}


# ---- 分析 ----

def _selector(value, count: int, label: str) -> list[int]:
    if value is None or str(value).lower() == "all":
        return list(range(1, count + 1))
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise SaosError(ErrorCode.BAD_ARGS, f"{label} must be 'all' or a number") from exc
    if not 1 <= n <= count:
        raise SaosError(ErrorCode.BAD_ARGS, f"{label} {n} is out of range 1–{count}")
    return [n]


def _check_settings(settings: dict[str, Any], allowed: tuple[str, ...]) -> None:
    unknown = sorted(set(settings) - set(allowed))
    if unknown:
        raise SaosError(ErrorCode.BAD_ARGS, f"Optiland analysis has no setting {unknown[0]!r}", {"settings": list(allowed)})


def _hexapolar(rings: int) -> list[tuple[float, float]]:
    points = [(0.0, 0.0)]
    for r in range(1, rings + 1):
        for k in range(6 * r):
            angle = 2 * math.pi * k / (6 * r)
            points.append((r / rings * math.cos(angle), r / rings * math.sin(angle)))
    return points


def _finite_list(values) -> list[float | None]:
    return [float(v) if math.isfinite(float(v)) else None for v in values]


def _spot(b, settings):
    _check_settings(settings, ("Field", "Wavelength", "RayDensity", "ReferTo"))
    fields = b._normalized_fields()
    waves = _selector(settings.get("Wavelength"), b.wavelength_count(), "Wavelength")
    chosen = _selector(settings.get("Field"), len(fields), "Field")
    density = int(settings.get("RayDensity", 6))
    refer = str(settings.get("ReferTo", "ChiefRay"))
    if refer not in ("ChiefRay", "Centroid") or not 1 <= density <= 40:
        raise SaosError(ErrorCode.BAD_ARGS, "ReferTo must be ChiefRay or Centroid and RayDensity 1–40")
    pupil = _hexapolar(density)
    weights = b._effective["wavelengths"]["weights"]
    primary = b.primary_wave()
    rms = [[None] * b.wavelength_count() for _ in fields]
    geo = [[None] * b.wavelength_count() for _ in fields]
    poly_rms, poly_geo, header = {}, {}, [f"Spot diagram, reference {refer}, hexapolar density {density}, units um"]
    image = len(b._rows) - 1
    for f in chosen:
        hx, hy = fields[f - 1]
        chief = b.trace_batch([(primary, hx, hy, 0.0, 0.0)], image)
        xs, ys, ws = [], [], []
        for w in waves:
            traced = b.trace_batch([(w, hx, hy, px, py) for px, py in pupil], image)
            keep = traced.ok
            if keep.sum() == 0:
                continue
            x, y = traced.x[keep], traced.y[keep]
            if refer == "Centroid":
                cx, cy = x.mean(), y.mean()
            elif chief.ok[0]:
                cx, cy = chief.x[0], chief.y[0]
            else:
                continue
            r2 = (x - cx) ** 2 + (y - cy) ** 2
            rms[f - 1][w - 1] = 1000 * float(np.sqrt(r2.mean()))
            geo[f - 1][w - 1] = 1000 * float(np.sqrt(r2.max()))
            xs.append(x), ys.append(y), ws.append(np.full(len(x), weights[w - 1] / len(x)))
        if xs:
            x, y, wt = np.concatenate(xs), np.concatenate(ys), np.concatenate(ws)
            if wt.sum() > 0:
                if refer == "Centroid":
                    cx, cy = np.dot(wt, x) / wt.sum(), np.dot(wt, y) / wt.sum()
                elif chief.ok[0]:
                    cx, cy = chief.x[0], chief.y[0]
                else:
                    continue
                r2 = (x - cx) ** 2 + (y - cy) ** 2
                poly_rms[f] = 1000 * float(np.sqrt(np.dot(wt, r2) / wt.sum()))
                poly_geo[f] = 1000 * float(np.sqrt(r2.max()))
                header.append(f"Field {f}: RMS {poly_rms[f]:.6g} um, GEO {poly_geo[f]:.6g} um")
    return {"header": header, "series": [], "grids": [],
            "spot": {"rms_radius": rms, "geo_radius": geo, "index": "[field-1][wave-1]", "units": "um",
                     "polychromatic": {"rms_radius": poly_rms, "geo_radius": poly_geo},
                     "note": "per-wavelength values are each wavelength's own spot; polychromatic weights wavelengths by their weights"}}, \
        {"Field": settings.get("Field", "all"), "Wavelength": settings.get("Wavelength", "all"), "RayDensity": density,
         "ReferTo": refer, "Pattern": "Hexapolar"}


def _mtf(b, settings):
    from optiland.mtf import FFTMTF

    _check_settings(settings, ("Field", "Wavelength", "MaximumFrequency", "SampleSize", "ShowDiffractionLimit", "Type"))
    if settings.get("Type", "Modulation") != "Modulation":
        raise unsupported("Optiland FFT MTF gives the modulation only")
    if str(settings.get("Wavelength", "")).lower() == "all":
        raise unsupported("polychromatic FFT MTF has not been mapped; give one Wavelength number")
    wave = int(settings.get("Wavelength", b.primary_wave()))
    _selector(wave, b.wavelength_count(), "Wavelength")
    fields = b._normalized_fields()
    chosen = _selector(settings.get("Field"), len(fields), "Field")
    size = SAMPLE_SIZES.get(str(settings.get("SampleSize", "S_64x64")))
    if size is None:
        raise SaosError(ErrorCode.BAD_ARGS, f"SampleSize must be one of {list(SAMPLE_SIZES)}")
    um = b._effective["wavelengths"]["um"][wave - 1]
    native = [tuple(map(float, b._native_fields(*fields[f - 1]))) for f in chosen]
    max_freq = settings.get("MaximumFrequency")
    try:
        result = FFTMTF(b.optic, fields=native, wavelength=um, num_rays=size, max_freq="cutoff" if max_freq in (None, 0, 0.0) else float(max_freq))
    except (ValueError, ZeroDivisionError, FloatingPointError) as exc:
        raise SaosError(ErrorCode.OPTILAND_ERROR, f"FFT MTF failed: {exc}") from exc
    limit = float(result.max_freq)
    series = []
    for k, f in enumerate(chosen):
        # 与 OpticStudio 的导出一致：每个视场一条序列、子午和弧矢两列；Optiland 两个方向的频率轴略有不同，弧矢插值到子午的频率上
        freq = np.asarray(array(result.freq_tang[k])).ravel()
        tangential = np.asarray(array(result.mtf[k][0])).ravel()
        sagittal = np.interp(freq, np.asarray(array(result.freq_sag[k])).ravel(), np.asarray(array(result.mtf[k][1])).ravel(),
                             right=np.nan)
        keep = freq <= limit + 1e-12
        series.append({"description": f"Field {f}", "x_label": "Spatial Frequency in cycles per mm", "x": freq[keep].tolist(),
                       "y": {"Tangential": _finite_list(tangential[keep]), "Sagittal": _finite_list(sagittal[keep])}})
    if settings.get("ShowDiffractionLimit", True):
        fno = float(np.asarray(array(result.FNO[0])).ravel()[0])
        cutoff = 1 / (um * 1e-3 * fno)
        nu = np.linspace(0, limit, 101)
        v = np.clip(nu / cutoff, 0, 1)
        series.append({"description": "Diffraction Limit", "x_label": "Spatial Frequency in cycles per mm", "x": nu.tolist(),
                       "y": {"Diffraction Limit": (2 / math.pi * (np.arccos(v) - v * np.sqrt(1 - v * v))).tolist()}})
    return {"header": [f"FFT MTF, wavelength {wave} ({um} um), pupil sampling {size}x{size}"], "series": series, "grids": []}, \
        {"Field": settings.get("Field", "all"), "Wavelength": wave, "MaximumFrequency": limit, "SampleSize": f"S_{size}x{size}",
         "Type": "Modulation", "ShowDiffractionLimit": bool(settings.get("ShowDiffractionLimit", True))}


def _ray_fan(b, settings):
    _check_settings(settings, ("Field", "Wavelength", "NumberOfRays"))
    fields = b._normalized_fields()
    chosen = _selector(settings.get("Field"), len(fields), "Field")
    waves = _selector(settings.get("Wavelength"), b.wavelength_count(), "Wavelength")
    count = int(settings.get("NumberOfRays", 20))
    if not 1 <= count <= 1000:
        raise SaosError(ErrorCode.BAD_ARGS, "NumberOfRays must be 1–1000")
    pupil = np.linspace(-1, 1, 2 * count + 1)
    image, primary = len(b._rows) - 1, b.primary_wave()
    series = []
    for f in chosen:
        hx, hy = fields[f - 1]
        chief = b.trace_batch([(primary, hx, hy, 0.0, 0.0)], image)
        for name, axis in (("Tangential", "y"), ("Sagittal", "x")):
            curves = {}
            for w in waves:
                rays = [(w, hx, hy, 0.0, p) if axis == "y" else (w, hx, hy, p, 0.0) for p in pupil]
                traced = b.trace_batch(rays, image)
                values = (getattr(traced, axis) - (getattr(chief, axis)[0] if chief.ok[0] else np.nan)) * 1000
                curves[f"{b._effective['wavelengths']['um'][w - 1]:.4f}"] = _finite_list(np.where(traced.ok, values, np.nan))
            series.append({"description": f"Field {f}: {name} (e{axis} vs p{axis}, um)", "x_label": f"P{axis}", "x": pupil.tolist(), "y": curves})
    return {"header": ["Transverse ray fan relative to the primary-wavelength chief ray, units um"], "series": series, "grids": []}, \
        {"Field": settings.get("Field", "all"), "Wavelength": settings.get("Wavelength", "all"), "NumberOfRays": count}


def _field_curvature(b, settings):
    from optiland.analysis import Distortion, FieldCurvature

    _check_settings(settings, ("Wavelength", "NumberOfPoints"))
    waves = _selector(settings.get("Wavelength"), b.wavelength_count(), "Wavelength")
    points = int(settings.get("NumberOfPoints", 64))
    um = [b._effective["wavelengths"]["um"][w - 1] for w in waves]
    if b._effective["fields"].get("normalization", "radial") != "radial" or any(x for x in b._effective["fields"]["x"]):
        raise unsupported("field curvature and distortion are mapped for y fields with radial normalization only")
    try:
        curvature = FieldCurvature(b.optic, wavelengths=um, num_points=points)
        distortion = Distortion(b.optic, wavelengths=um, num_points=points, distortion_type="f-tan")
    except (ValueError, ZeroDivisionError, FloatingPointError) as exc:
        raise SaosError(ErrorCode.OPTILAND_ERROR, f"field curvature/distortion failed: {exc}") from exc
    max_field = float(np.max(np.hypot(b._effective["fields"]["x"], b._effective["fields"]["y"])))
    series = []
    for k, w in enumerate(waves):
        tangential, sagittal = (np.asarray(array(v)).ravel() for v in curvature.data[k])
        y = np.linspace(0, max_field, len(tangential))
        series.append({"description": f"Field curvature, wavelength {w}", "x_label": "Field",
                       "x": y.tolist(), "y": {"Tangential": _finite_list(tangential), "Sagittal": _finite_list(sagittal)}})
    for k, w in enumerate(waves):
        values = np.asarray(array(distortion.data[k])).ravel()
        series.append({"description": f"Distortion (F-Tan, %), wavelength {w}", "x_label": "Field",
                       "x": np.linspace(1e-10 * max_field, max_field, len(values)).tolist(), "y": {"Distortion": _finite_list(values)}})
    return {"header": ["Field curvature in lens units from the paraxial image plane; distortion in percent (F-Tan)"],
            "series": series, "grids": []}, {"Wavelength": settings.get("Wavelength", "all"), "NumberOfPoints": points, "Distortion": "F_TanTheta"}


def _wavefront_map(b, settings):
    _check_settings(settings, ("Field", "Wavelength", "Sampling"))
    fields = b._normalized_fields()
    field = _selector(settings.get("Field", 1), len(fields), "Field")[0]
    wave = _selector(settings.get("Wavelength", b.primary_wave()), b.wavelength_count(), "Wavelength")[0]
    size = SAMPLE_SIZES.get(str(settings.get("Sampling", "S_64x64")))
    if size is None:
        raise SaosError(ErrorCode.BAD_ARGS, f"Sampling must be one of {list(SAMPLE_SIZES)}")
    axis = np.linspace(-1, 1, size)
    gx, gy = np.meshgrid(axis, axis)
    inside = gx * gx + gy * gy <= 1
    x, y, w = opd_samples(b, wave, *fields[field - 1], gx[inside], gy[inside])
    grid = np.full((size, size), np.nan)
    ix = np.rint((x + 1) / 2 * (size - 1)).astype(int)
    iy = np.rint((y + 1) / 2 * (size - 1)).astype(int)
    grid[iy, ix] = w
    step = max(1, math.ceil(size / 256))
    rows = [[float(v) if math.isfinite(v) else None for v in row[::step]] for row in grid[::step]]
    flat = w.tolist()
    return {"header": [f"Wavefront map, field {field}, wavelength {wave}, reference chief ray, units waves"], "series": [],
            "grids": [{"description": f"Wavefront field {field} wave {wave}", "nx": size, "ny": size, "dx": 2 / (size - 1),
                       "dy": 2 / (size - 1), "min_x": -1.0, "min_y": -1.0, "value_label": "waves", "step": step, "values": rows,
                       "stats": {"min": min(flat), "max": max(flat), "mean": sum(flat) / len(flat)} if flat else None}]}, \
        {"Field": field, "Wavelength": wave, "Sampling": f"S_{size}x{size}", "ReferenceToPrimary": False}


def seidel_terms(b, wave: int) -> np.ndarray:
    """逐面 Seidel 系数 S1–S5（镜头单位），形状 (面数-2, 5)，第 k 行是面 k+1。"""
    from optiland.aberrations.third_order import ThirdOrderAberrations

    before = b.optic.wavelengths.primary_index
    try:
        b.optic.wavelengths.primary_index = wave - 1
        third = ThirdOrderAberrations(b.optic)
        third._precalculations()
        terms = [third._compute_over_surfaces(getattr(third, f"_{name}_term")) for name in ("TSC", "CC", "TAC", "TPC", "DC")]
        factor = float(np.asarray(array(third._n[-1] * third._ua[-1] * 2)).ravel()[0])
        return np.column_stack([-np.asarray(array(t)).ravel() * factor for t in terms])
    finally:
        b.optic.wavelengths.primary_index = before


def _seidel(b, settings):
    _check_settings(settings, ("Wavelength",))
    wave = _selector(settings.get("Wavelength", b.primary_wave()), b.wavelength_count(), "Wavelength")[0]
    s = seidel_terms(b, wave)
    columns = ["Surf", "SPHA S1", "COMA S2", "ASTI S3", "FCUR S4", "DIST S5"]
    rows = [[k + 1, *map(float, row)] for k, row in enumerate(s)] + [["TOT", *map(float, s.sum(axis=0))]]
    return {"header": [f"Seidel coefficients in lens units, wavelength {wave}"], "series": [], "grids": [],
            "tables": [{"columns": columns, "rows": rows}]}, {"Wavelength": wave}


RUNNERS = {"StandardSpot": _spot, "FftMtf": _mtf, "RayFan": _ray_fan, "FieldCurvatureAndDistortion": _field_curvature,
           "WavefrontMap": _wavefront_map, "SeidelCoefficients": _seidel}


def analysis(b, name: str, settings: dict[str, Any], text_path: Path) -> dict[str, Any]:
    if name not in RUNNERS:
        raise unsupported(f"analysis {name} has not been mapped to Optiland; mapped: {', '.join(ANALYSES)}")
    out, used = RUNNERS[name](b, dict(settings))
    out.update(messages=[], problems=[], settings={k: str(v) for k, v in used.items()}, message=None, backend="optiland")
    has_data = bool(out.get("series") or out.get("grids") or out.get("spot") or out.get("tables"))
    out["status"] = "ok" if has_data else "empty"
    text_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"saos Optiland {name}", *out["header"], *(f"{k} = {v}" for k, v in out["settings"].items())]
    for table in out.get("tables", []):
        lines.append("\t".join(table["columns"]))
        lines.extend("\t".join(str(v) for v in row) for row in table["rows"])
    for s in out["series"]:
        lines.append(f"# {s['description']}")
        labels = list(s["y"])
        lines.append("\t".join([s["x_label"], *labels]))
        lines.extend("\t".join(str(v) for v in [x, *(s["y"][label][i] for label in labels)]) for i, x in enumerate(s["x"]))
    text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out["text_file"] = True
    return out
