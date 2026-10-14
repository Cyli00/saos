"""镜头处方：从零搭建一个序列系统所需的全部数据，以及 saos 的标准设计布局。

标准设计布局（lens build 生成，验收评估据此找出物镜部分）：

    面 0      物面（有盖玻片时介质为盖玻片，厚度为盖玻片厚度）       注释 saos:object
    面 1      盖玻片上表面，厚度为工作距离间隔（仅有盖玻片时）       注释 saos:cover_top
    ...       物镜各面（可含注释为 saos:stop 的虚拟光阑面）
    面 k      参考筒镜：Paraxial 面，焦距 f_TL，后接 f_TL 到像面      注释 saos:tube
    面 k+1    像面

没有盖玻片时，物面厚度就是工作距离间隔。
"""

from __future__ import annotations

import math
from typing import Any

from saos.protocol import ErrorCode, SaosError

TAG_OBJECT = "saos:object"
TAG_COVER_TOP = "saos:cover_top"
TAG_STOP = "saos:stop"
TAG_TUBE = "saos:tube"

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
        "comment": str(obj.get("comment", TAG_OBJECT)),
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


def objective_block(rows: list[dict[str, Any]]) -> tuple[int, int]:
    """在标准设计布局的面列表中找出物镜部分，返回 [first, last]（面序号，含两端）。"""
    comments = [str(r.get("comment", "")) for r in rows]
    if not comments or comments[0] != TAG_OBJECT:
        raise SaosError(ErrorCode.BAD_ARGS, "不是 saos 标准设计布局：物面注释不是 saos:object")
    try:
        tube = comments.index(TAG_TUBE)
    except ValueError as exc:
        raise SaosError(ErrorCode.BAD_ARGS, "不是 saos 标准设计布局：找不到 saos:tube 参考筒镜") from exc
    first = 2 if len(comments) > 1 and comments[1] == TAG_COVER_TOP else 1
    if tube <= first:
        raise SaosError(ErrorCode.BAD_ARGS, "标准设计布局中物镜部分为空")
    return first, tube - 1


def gap_surface(rows: list[dict[str, Any]]) -> int:
    """工作距离间隔所在的面（其厚度即物方调焦量）。"""
    return 1 if len(rows) > 1 and rows[1].get("comment") == TAG_COVER_TOP else 0
