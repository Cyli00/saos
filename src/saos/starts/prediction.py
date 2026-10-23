"""用 OpticStudio 的操作数和原生分析生成起点预判，不自建光学求解器。"""
from __future__ import annotations

import math
import tempfile
from pathlib import Path

from saos import jobs, model_gate
from saos.backend.baseline import normalized_fields
from saos.protocol import ErrorCode, SaosError, jsonable

COLUMNS = ("SPHA", "COMA", "ASTI", "FCUR", "DIST")
CONVENTIONS = {
    "length": "OpticStudio 当前镜头单位",
    "third_order": "SPHA/COMA/ASTI/FCUR/DIST 原生操作数，单位 waves；不是未换算的 SI–SV",
    "native_analysis": "完整三阶及色差表保存在 SeidelCoefficients 原生文本，保留 OpticStudio 的单位与表头",
    "incidence": "RAID，度；仅统计成功到达对应面的采样光线，不代表连续最大值",
    "pupils": "入瞳相对首面，出瞳相对像面；stop_z 是后端全局坐标，bfl 是末面近轴焦距",
}


def predict(backend, *, rings: int = 3, arms: int = 8, should_cancel=None) -> dict:
    if (isinstance(rings, bool) or not isinstance(rings, int) or not 1 <= rings <= 20
            or isinstance(arms, bool) or not isinstance(arms, int) or not 4 <= arms <= 64):
        raise ValueError("入射角采样要求 1–20 环、4–64 个方位")
    info, rows = backend.system_info(), backend.surfaces()
    if info.get("mode") == "non_sequential":
        raise SaosError(ErrorCode.BACKEND_UNSUPPORTED, "starting-design prediction requires a sequential system")
    desc = backend.describe_model(None)
    identity = backend.identity()
    out = {"backend": identity["backend"], "version": identity["version"], "time": jobs.now_iso(),
           "fingerprint": model_gate.digest(model_gate.fingerprint(desc)),
           "model_sha": model_gate.digest({k: v for k, v in desc.items() if k not in ("file", "layout")}),
           "conventions": CONVENTIONS, "configuration": backend.current_configuration(), "errors": {}}

    def checkpoint():
        if should_cancel is not None and should_cancel():
            raise SaosError(ErrorCode.CANCELLED, "starting-design prediction cancelled")

    def failed(key, exc):
        if isinstance(exc, SaosError) and exc.code in (ErrorCode.ZOS_CONNECTION_LOST, ErrorCode.UI_BLOCKED, ErrorCode.CANCELLED):
            raise exc
        out["errors"][key] = f"{type(exc).__name__}: {exc}"

    def op(name, surface=0, wave=0, data=()):
        checkpoint()
        value = float(backend.operand(name, surface, wave, list(data)))
        if not math.isfinite(value):
            raise ValueError(f"{name} 未返回有限值")
        return value

    primary = backend.primary_wave()
    first = {"stop_surface": info["stop_surface"]}
    for key, name, wave in (("efl", "EFFL", primary), ("total_track", "TOTR", 0),
                            ("entrance_pupil", "ENPP", 0), ("exit_pupil", "EXPP", 0),
                            ("entrance_pupil_diameter", "EPDI", 0), ("f_number", "ISFN", 0)):
        try:
            first[key] = op(name, wave=wave)
        except Exception as exc:
            first[key] = None
            failed(key, exc)
    try:
        _, position = backend.global_matrix(info["stop_surface"])
        first["stop_z"] = float(position[2])
    except Exception as exc:
        failed("stop_z", exc)
    try:
        # 在最后一个实体面，用后端近轴高度与斜率求相对该面的焦点距离。
        surface = len(rows) - 2
        height = op("PARY", surface, primary, (0, 0, 0, 1))
        slope = op("PATY", surface, primary, (0, 0, 0, 1))
        if not slope:
            raise ValueError("近轴出射光线平行，没有有限后焦距")
        first["bfl"] = -height / slope
    except Exception as exc:
        failed("bfl", exc)
    out["first_order"] = first
    centered = all(str(row.get("type", "standard")).lower() == "standard" and not row.get("tilt_decenter")
                   for row in desc.get("surfaces", [])[1:-1])
    try:
        if not centered:
            raise ValueError("该面型或偏心系统不使用同轴球面/圆锥面 Petzval 预判；请读原生分析")
        curvature = op("PETC", wave=primary)
        radius = 1.0 / curvature if curvature else None
        f = first.get("efl")
        out["petzval"] = {"radius": radius, "radius_over_efl": radius / f if radius is not None and f else None,
                          "reason": None if radius is not None else "Petzval 曲率为零，无有限半径"}
    except Exception as exc:
        failed("petzval", exc)
    try:
        if not centered:
            raise ValueError("该面型或偏心系统不使用同轴球面/圆锥面三阶预判；请读原生分析")
        # 逐面操作数与总和都由 OpticStudio 计算，不沿用另一后端的符号换算。
        values = [{"surface": i, **{name: op(name, i, primary) for name in COLUMNS}} for i in range(1, len(rows) - 1)]
        out["third_order"] = {"columns": list(COLUMNS), "units": "waves", "surfaces": values,
                              "totals": {name: op(name, 0, primary) for name in COLUMNS}}
    except Exception as exc:
        failed("third_order", exc)
    try:
        checkpoint()
        with tempfile.TemporaryDirectory(prefix="saos-seidel-") as folder:
            path = Path(folder) / "seidel.txt"
            data = backend.analysis("SeidelCoefficients", {"Wavelength": primary}, path)
            if not path.is_file():
                raise ValueError("OpticStudio 未写出 SeidelCoefficients 原生文本")
            from saos.backend.zos import _read_text_export
            out["native_analysis"] = {"type": "SeidelCoefficients", "wavelength": primary,
                                      "text": _read_text_export(path), "messages": data.get("messages", [])}
    except Exception as exc:
        failed("native_analysis", exc)
    pupil = [(0.0, 0.0)] + [(r / rings * math.cos(a * 2 * math.pi / arms), r / rings * math.sin(a * 2 * math.pi / arms))
                           for r in range(1, rings + 1) for a in range(arms)]
    fields = normalized_fields(info)
    sample_count = len(info["wavelengths"]) * len(fields) * len(pupil)
    if sample_count * max(1, len(rows) - 1) > 100000:
        raise SaosError(ErrorCode.BAD_ARGS, "prediction exceeds 100000 ray-surface samples; reduce rings/arms or system sampling")
    rays = [(wave, hx, hy, px, py) for wave in range(1, len(info["wavelengths"]) + 1)
            for hx, hy in fields for px, py in pupil]
    incidence = {"sampling": {"rings": rings, "arms": arms, "fields": "all configured", "waves": "all configured"},
                 "attempted": len(rays), "invalid": None, "continuous_maximum_verified": False, "surfaces": []}
    for surface in range(1, len(rows)):
        checkpoint()
        try:
            traced = backend.trace_batch(rays, surface)
            angles = [op("RAID", surface, int(ray[0]), ray[1:]) for ray, valid in zip(rays, traced.ok) if valid]
            incidence["surfaces"].append({"surface": surface, "max_deg": max(angles) if angles else None,
                                          "valid_samples": len(angles)})
            if surface == len(rows) - 1:
                incidence["invalid"] = len(rays) - int(sum(traced.ok))
        except Exception as exc:
            failed(f"incidence.{surface}", exc)
            incidence["surfaces"].append({"surface": surface, "max_deg": None, "valid_samples": 0})
    out["incidence"] = incidence
    return jsonable(out)
