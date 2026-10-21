"""按统一长度比例缩放序列处方，输入错误统一返回 BAD_ARGS。"""
from __future__ import annotations

import copy
import math
from typing import Any

from saos.prescription import APERTURES, FIELDS, asphere_parameter_order, canonical_surface_params
from saos.protocol import ErrorCode, SaosError


def _bad(message: str):
    return SaosError(ErrorCode.BAD_ARGS, message)


def _mapping(value, label, *, optional=False):
    if value is None and optional:
        return {}
    if not isinstance(value, dict):
        raise _bad(f"{label} 必须为对象")
    return value


def _number(value, label, *, infinity=False):
    if infinity and isinstance(value, str) and value.lower() in ("inf", "infinity", "-inf", "-infinity"):
        return float(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _bad(f"{label} 必须为数值，不能是字符串或布尔值")
    try:
        invalid = math.isnan(value) or (not infinity and not math.isfinite(value))
    except OverflowError as exc:
        raise _bad(f"{label} 超出数值范围") from exc
    if invalid:
        raise _bad(f"{label} 必须为有限数值")
    return value


def _factor(factor):
    value = _number(factor, "缩放比例")
    if value <= 0:
        raise _bad("缩放比例必须为正数")
    return value


def _scaled(value, factor, exponent, label, *, infinity=False):
    value = _number(value, label, infinity=infinity)
    try:
        result = float(value) * float(factor) ** exponent
    except (OverflowError, ZeroDivisionError) as exc:
        raise _bad(f"{label} 的缩放超出数值范围") from exc
    if math.isfinite(value) and not math.isfinite(result):
        raise _bad(f"{label} 的缩放超出数值范围")
    return result


def _power(kind: str, name: str) -> int:
    if not isinstance(name, str) or not name.strip():
        raise _bad("面型参数名必须为非空字符串")
    order = asphere_parameter_order(kind, name)
    if order is not None:
        return 1 - order
    key = name.lower().replace(" ", "")
    kind = kind.lower().replace(" ", "")
    if kind == "coordinatebreak":
        if key in ("par1", "par2", "decenterx", "decentery"):
            return 1
        if key in ("par3", "par4", "par5", "par6", "tiltaboutx", "tiltabouty", "tiltaboutz", "order"):
            return 0
    if kind == "paraxial" and key in ("par1", "par2"):
        return 1 if key == "par1" else 0
    raise _bad(f"不能确定 {kind}.{name} 的缩放量纲")


def scale_surface(surface: dict[str, Any], factor: float) -> dict[str, Any]:
    factor = _factor(factor)
    out = copy.deepcopy(_mapping(surface, "面"))
    if out.get("objects"):
        raise _bad("混合模式中的非序列物体尚无统一缩放映射")
    kind = out.get("type", "standard")
    if not isinstance(kind, str):
        raise _bad("面型必须为字符串")
    for key in ("radius", "thickness", "semi_diameter", "mech_semi_diameter", "focal_length", "conic", "opd_mode"):
        if key in out:
            exponent = 0 if key in ("conic", "opd_mode") else 1
            out[key] = _scaled(out[key], factor, exponent, key, infinity=key in ("radius", "thickness"))
    params = _mapping(out.get("params"), "params", optional=True)
    for key, value in params.items():
        params[key] = _scaled(value, factor, _power(kind, key), f"params.{key}")
    if "params" in out:
        out["params"] = canonical_surface_params(kind, params)
    transforms = _mapping(out.get("decenter_tilt"), "decenter_tilt", optional=True)
    for position, part in transforms.items():
        if part is None or (position == "after" and part == "reverse"):
            continue
        _mapping(part, f"decenter_tilt.{position}")
        for key in ("decenter_x", "decenter_y", "tilt_x", "tilt_y", "tilt_z"):
            if key in part:
                part[key] = _scaled(part[key], factor, 1 if key.startswith("decenter") else 0, key)
    aperture = _mapping(out.get("aperture"), "aperture", optional=True)
    for key in ("min", "max", "x_half", "y_half", "x_decenter", "y_decenter", "width", "arms"):
        if key in aperture:
            aperture[key] = _scaled(aperture[key], factor, 0 if key == "arms" else 1, f"aperture.{key}")
    solves = _mapping(out.get("solves"), "solves", optional=True)
    for column, solve in solves.items():
        if solve == "variable":
            continue
        _mapping(solve, f"solves.{column}")
        for key in ("scale", "zone", "value"):
            if key in solve:
                _number(solve[key], f"solves.{column}.{key}")
        if solve.get("type") == "pickup" and "offset" in solve:
            exponent = 1 if column in ("radius", "thickness") else 0 if column == "conic" else _power(kind, column)
            solve["offset"] = _scaled(solve["offset"], factor, exponent, f"solves.{column}.offset")
        if solve.get("type") == "marginal_ray_height" and "height" in solve:
            solve["height"] = _scaled(solve["height"], factor, 1, "solves.height")
    return out


def scale_prescription(data: dict[str, Any], factor: float) -> dict[str, Any]:
    factor = _factor(factor)
    data = _mapping(data, "处方")
    if data.get("mode", "sequential") != "sequential" or data.get("configurations"):
        raise _bad("通用缩放当前支持单配置序列处方；非序列与多配置的参数量纲尚未登记")
    if not isinstance(data.get("surfaces"), list) or not data["surfaces"]:
        raise _bad("surfaces 必须为非空面列表")
    out = copy.deepcopy(data)
    out["surfaces"] = [scale_surface(s, factor) for s in out["surfaces"]]
    for key in ("object", "image"):
        if out.get(key) is not None:
            out[key] = scale_surface(out[key], factor)
    ap = _mapping(out.get("aperture"), "系统孔径", optional=True)
    if ap and (not isinstance(ap.get("type"), str) or ap.get("type") not in APERTURES or (ap.get("type") != "float_by_stop" and ap.get("value") is None)):
        raise _bad("系统孔径需要已知 type 和数值 value")
    if ap.get("value") is not None:
        ap["value"] = _scaled(ap["value"], factor, 1 if ap.get("type") == "entrance_pupil_diameter" else 0, "aperture.value")
    fields = _mapping(out.get("fields"), "fields", optional=True)
    if fields and (not isinstance(fields.get("type"), str) or fields.get("type") not in FIELDS):
        raise _bad("fields.type 未知，无法确定视场的缩放量纲")
    for key in ("x", "y", "weights"):
        if key in fields:
            if not isinstance(fields[key], list):
                raise _bad(f"fields.{key} 必须为数值列表")
            exponent = int(key != "weights" and fields.get("type") in ("object_height", "real_image_height", "paraxial_image_height"))
            fields[key] = [_scaled(v, factor, exponent, f"fields.{key}") for v in fields[key]]
    waves = _mapping(out.get("wavelengths"), "wavelengths", optional=True)
    for key in ("um", "weights"):
        if key in waves:
            if not isinstance(waves[key], list):
                raise _bad(f"wavelengths.{key} 必须为数值列表")
            for value in waves[key]:
                _number(value, f"wavelengths.{key}")
    return out
