"""镜头处方：从零搭建一个序列系统所需的数据（lens build 的输入格式）。

目前支持 standard 与 paraxial 两种面型、物方 NA 孔径和物高视场。
"""


from __future__ import annotations

import math
from typing import Any

from saos.protocol import ErrorCode, SaosError

RAY_AIMING = ("off", "paraxial", "real")


def _bad(message: str) -> SaosError:
    return SaosError(ErrorCode.BAD_ARGS, f"处方错误：{message}")


def _number(value: Any, name: str) -> float:
    if isinstance(value, str) and value.lower() in ("inf", "infinity"):
        return math.inf
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _bad(f"{name} 需要数值，收到 {value!r}")
    return float(value)


def normalize_material(value: Any, name: str) -> Any:
    """材料：'' 为空气；字符串为目录牌号；{"model": {"nd", "vd", "dpgf"}} 为模型玻璃。"""
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value.strip().upper()
    if isinstance(value, dict) and isinstance(value.get("model"), dict):
        model = value["model"]
        return {"model": {"nd": _number(model.get("nd"), f"{name}.nd"), "vd": _number(model.get("vd"), f"{name}.vd"), "dpgf": float(model.get("dpgf", 0.0))}}
    raise _bad(f"{name} 的材料格式不对：{value!r}")


def normalize(p: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(p, dict):
        raise _bad("处方必须是 JSON 对象")
    aperture = p.get("aperture") or {}
    if aperture.get("type") != "object_na":
        raise _bad("aperture.type 目前只支持 object_na")
    fields = p.get("fields") or {}
    if fields.get("type") != "object_height":
        raise _bad("fields.type 目前只支持 object_height")
    ys = [_number(y, "fields.y") for y in fields.get("y") or []]
    if not ys:
        raise _bad("至少需要一个视场")
    waves = p.get("wavelengths") or {}
    um = [_number(w, "wavelengths.um") for w in waves.get("um") or []]
    if not um:
        raise _bad("至少需要一个波长")
    weights = [_number(w, "wavelengths.weights") for w in waves.get("weights") or [1.0] * len(um)]
    primary = waves.get("primary", 1)
    if len(weights) != len(um) or not isinstance(primary, int) or not 1 <= primary <= len(um):
        raise _bad("wavelengths 的权重个数或主波长序号不对")
    aiming = p.get("ray_aiming", "real")
    if aiming not in RAY_AIMING:
        raise _bad(f"ray_aiming 只能是 {RAY_AIMING}")

    obj = p.get("object") or {}
    out_obj = {
        "thickness": _number(obj.get("thickness"), "object.thickness"),
        "material": normalize_material(obj.get("material", ""), "object"),
        "comment": str(obj.get("comment", "")),
    }

    surfaces = []
    stops = 0
    for k, s in enumerate(p.get("surfaces") or [], start=1):
        name = f"surfaces[{k}]"
        kind = s.get("type", "standard")
        if kind not in ("standard", "paraxial"):
            raise _bad(f"{name}.type 只能是 standard / paraxial")
        item: dict[str, Any] = {
            "type": kind,
            "thickness": _number(s.get("thickness", 0.0), f"{name}.thickness"),
            "comment": str(s.get("comment", "")),
            "stop": bool(s.get("stop", False)),
        }
        if kind == "standard":
            item["radius"] = _number(s.get("radius", math.inf), f"{name}.radius")
            item["conic"] = _number(s.get("conic", 0.0), f"{name}.conic")
            item["material"] = normalize_material(s.get("material", ""), name)
        else:
            item["focal_length"] = _number(s.get("focal_length"), f"{name}.focal_length")
            item["opd_mode"] = int(s.get("opd_mode", 1))
        stops += item["stop"]
        surfaces.append(item)
    if not surfaces:
        raise _bad("至少需要一个面")
    if stops > 1:
        raise _bad("只能有一个光阑面")

    return {
        "title": str(p.get("title", "")),
        "aperture": {"type": "object_na", "value": _number(aperture.get("value"), "aperture.value")},
        "fields": {"type": "object_height", "y": ys},
        "wavelengths": {"um": um, "weights": weights, "primary": primary},
        "catalogs": [str(c).upper() for c in p.get("catalogs") or ["SCHOTT"]],
        "ray_aiming": aiming,
        "telecentric_object": bool(p.get("telecentric_object", False)),
        "object": out_obj,
        "surfaces": surfaces,
    }
