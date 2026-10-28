"""CODE V 序列文件（.seq）的受控解析：只认专利镜头库（library/patents）实际出现的命令，其余原样列出，不猜含义。

parse 把文本读成结构；paraxial 用 nd 做近轴预筛（ADR-0013 的例外，同 starts.paraxial）；to_prescription 转成 lens build 的处方。
转换不替换任何材料：玻璃代码 nnnnnn.vvvvvv 转成模型玻璃（nd = 1.nnnnnn，vd = vv.vvvv），REFL 转成 MIRROR，
NAME_SCHOTT/HOYA/OHARA 转成该厂目录牌号；_SPECIAL 材料和 PRV 私有玻璃必须由调用方在 materials 里给出，否则拒绝。
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from saos import prescription as rx
from saos.protocol import ErrorCode, SaosError
from saos.starts.paraxial import Surface, system_matrix

TOKEN_RE = re.compile(r"'[^']*'|\"[^\"]*\"|[^\s;]+|;")
CODE_RE = re.compile(r"^(\d{6})\.(\d{6})$")
VENDORS = ("SCHOTT", "HOYA", "OHARA")
UNITS = {"M": "mm", "C": "cm", "I": "in"}
FIELD_KEYS = {"XAN": ("angle", "x"), "YAN": ("angle", "y"), "XOB": ("object_height", "x"), "YOB": ("object_height", "y"),
              "XIM": ("paraxial_image_height", "x"), "YIM": ("paraxial_image_height", "y")}
APERTURE_KEYS = {"EPD": "entrance_pupil_diameter", "FNO": "image_f_number", "NAO": "object_na", "NA": "image_na"}
VIGNETTING = ("VUX", "VLX", "VUY", "VLY")
# 非球面系数 A…J 依次是 r^4…r^20；偶次非球面 par2…par8 是 r^4…r^16
ASPHERE = {"A": 2, "B": 3, "C": 4, "D": 5, "E": 6, "F": 7, "G": 8, "H": 9, "J": 10}
# 物距不小于这个值按无穷远（库里写作 0.1e11 或 9.99e10）
INFINITE_OBJECT = 1e9
# 认识、但转换时不带过去的命令：优化控制（CCY/THC/KC）与 CODE V 的渐晕系数、通光口径
CONTROLS = {"CCY", "THC", "KC"}


def _bad(message: str, **details) -> SaosError:
    return SaosError(ErrorCode.BAD_ARGS, message, details or None)


def _commands(line: str) -> list[list[str]]:
    out, current = [], []
    for token in TOKEN_RE.findall(line):
        if token == ";":
            if current:
                out.append(current)
            current = []
        else:
            current.append(token)
    if current:
        out.append(current)
    return out


def _floats(tokens: list[str], where: str) -> list[float]:
    try:
        return [float(t) for t in tokens]
    except ValueError as exc:
        raise _bad(f"{where}: 不是数值 {tokens}") from exc


def _unquote(token: str) -> str:
    return token[1:-1] if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"" else token


def material(token: str | None, private: dict[str, Any]) -> dict[str, Any]:
    """面之后介质的分类；kind 为 air/code/mirror/catalog/private/special/unknown。"""
    if token is None:
        return {"kind": "air"}
    if token.upper() == "REFL":
        return {"kind": "mirror", "raw": token}
    m = CODE_RE.match(token)
    if m:
        return {"kind": "code", "raw": token, "nd": 1 + int(m[1]) / 1e6, "vd": int(m[2]) / 1e4}
    if token[0] in "'\"":
        name = _unquote(token)
        return {"kind": "private", "raw": name, "defined": name in private}
    name, _, vendor = token.rpartition("_")
    if vendor == "SPECIAL" and name:
        return {"kind": "special", "raw": token}
    if vendor in VENDORS and name:
        return {"kind": "catalog", "raw": token, "name": name, "vendor": vendor}
    return {"kind": "unknown", "raw": token}


def parse(text: str, name: str = "") -> dict[str, Any]:
    """把 .seq 文本读成结构。不认识的命令放进 unrecognized，认识但不转换的放进 not_converted。"""
    data: dict[str, Any] = {"name": name, "title": "", "units": None, "wavelengths_nm": [], "reference": None, "wave_weights": None,
                            "aperture": None, "fields": None, "field_weights": None, "vignetting": {}, "private": {}, "object": None,
                            "surfaces": [], "image": None, "zoom": None, "afocal_image": None, "radius_mode": False,
                            "not_converted": [], "unrecognized": [], "unsupported": []}
    fields: dict[str, list[float]] = {}
    field_type = None
    phase = "spec"
    private_waves = None
    surface = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("!"):
            continue
        for cmd in _commands(line):
            key, args = cmd[0].upper(), cmd[1:]
            where = f"{name}: {' '.join(cmd)}"
            if phase == "private":
                if key == "END":
                    phase = "spec"
                elif key == "PWL":
                    private_waves = _floats(args, where)
                elif cmd[0][0] in "'\"" and private_waves:
                    data["private"][_unquote(cmd[0])] = {"wavelengths_nm": private_waves, "indices": _floats(args, where)}
                else:
                    data["unrecognized"].append(" ".join(cmd))
                continue
            if key == "RDM":
                data["radius_mode"] = True
            elif key == "LEN" or key == "GO" or key == "INI":
                pass
            elif key in ("TITLE", "TIT"):
                data["title"] = " ".join(_unquote(a) for a in args).strip()
            elif key == "DIM" and args and args[0].upper() in UNITS:
                data["units"] = UNITS[args[0].upper()]
            elif key == "WL":
                data["wavelengths_nm"] = _floats(args, where)
            elif key == "REF" and len(args) == 1:
                data["reference"] = int(args[0])
            elif key == "WTW":
                data["wave_weights"] = _floats(args, where)
            elif key == "WTF":
                data["field_weights"] = _floats(args, where)
            elif key in APERTURE_KEYS and len(args) == 1 and phase == "spec":
                if data["aperture"] is not None:
                    data["unrecognized"].append(" ".join(cmd))
                data["aperture"] = {"type": APERTURE_KEYS[key], "value": float(args[0])}
            elif key in FIELD_KEYS and phase == "spec":
                kind, axis = FIELD_KEYS[key]
                if field_type not in (None, kind):
                    data["unrecognized"].append(" ".join(cmd))
                field_type = kind
                fields[axis] = _floats(args, where)
            elif key in VIGNETTING and phase == "spec":
                data["vignetting"][key] = _floats(args, where)
            elif key == "PRV":
                phase = "private"
            elif key in ("SO", "S", "SI"):
                values = _floats(args[:2], where)
                if len(values) != 2 or len(args) > 3:
                    data["unrecognized"].append(" ".join(cmd))
                    continue
                surface = {"radius": values[0], "thickness": values[1], "token": args[2] if len(args) == 3 else None,
                           "stop": False, "pim": False, "conic": 0.0, "asphere": None}
                if key == "SO":
                    data["object"] = surface
                    phase = "surfaces"
                elif key == "S":
                    data["surfaces"].append(surface)
                else:
                    data["image"] = surface
                    phase = "after"
            elif phase == "surfaces" and surface is not None and key in ("STO", "PIM", "ASP", "CON"):
                if key == "STO":
                    surface["stop"] = True
                elif key == "PIM":
                    surface["pim"] = True
                elif key == "ASP":
                    surface["asphere"] = {}
                # CON 只表示二次曲面，系数由随后的 K 给出
            elif phase == "surfaces" and surface is not None and key == "K" and len(args) == 1:
                surface["conic"] = float(args[0])
            elif phase == "surfaces" and surface is not None and key in ASPHERE and len(args) == 1 and surface["asphere"] is not None:
                surface["asphere"][key] = float(args[0])
            elif phase == "surfaces" and key in ("IC", "CUF") and [a.upper() for a in args] in (["YES"], ["0.0"]):
                # 103 个非球面都写 IC Yes; CUF 0.0；含义未核实，只认这组取值
                data["not_converted"].append(" ".join(cmd))
            elif key in CONTROLS:
                data["not_converted"].append(" ".join(cmd))
            elif phase == "surfaces" and key == "CIR":
                data["not_converted"].append(f"S{len(data['surfaces'])} {' '.join(cmd)}")
            elif phase == "surfaces" and key.startswith("H") and key in ("HOE", "HV1", "HV2", "HOR", "HTH", "HIN", "HDI", "HSW", "HDN",
                                                                          "HX1", "HY1", "HZ1", "HX2", "HY2", "HZ2", "HWL", "HTO", "HCT"):
                if key == "HOE":
                    data["unsupported"].append(f"S{len(data['surfaces'])} 是全息面（HOE）")
            elif key == "ZOO":
                _zoo(data, cmd, where)
            elif key == "AFI" and len(args) == 1:
                # 无焦像空间：CODE V 用焦距为该值的理想透镜成像（见 README）
                data["afocal_image"] = float(args[0])
            else:
                data["unrecognized"].append(" ".join(cmd))
    if fields:
        count = max(len(v) for v in fields.values())
        data["fields"] = {"type": field_type, "x": fields.get("x", [0.0] * count), "y": fields.get("y", [0.0] * count)}
    return data


def _zoo(data: dict[str, Any], cmd: list[str], where: str) -> None:
    args = cmd[1:]
    zoom = data["zoom"]
    if len(args) == 1 and args[0].isdigit():
        data["zoom"] = {"positions": int(args[0]), "thickness": {}, "curvature": {}, "aperture": None, "fields": {}, "vignetting": {}}
        return
    if zoom is None or not args:
        data["unrecognized"].append(" ".join(cmd))
        return
    key, rest = args[0].upper(), args[1:]
    n = zoom["positions"]
    target = rest[0].upper() if rest else ""
    if key in ("THI", "CUY") and re.fullmatch(r"S\d+", target) and len(rest) == n + 1:
        zoom["thickness" if key == "THI" else "curvature"][int(target[1:])] = _floats(rest[1:], where)
    elif key in APERTURE_KEYS and len(rest) == n:
        zoom["aperture"] = {"type": APERTURE_KEYS[key], "values": _floats(rest, where)}
    elif key in FIELD_KEYS and re.fullmatch(r"F\d+", target) and len(rest) == n + 1:
        zoom["fields"].setdefault(key, {})[int(target[1:])] = _floats(rest[1:], where)
    elif key in VIGNETTING and re.fullmatch(r"F\d+", target):
        zoom["vignetting"].setdefault(key, {})[int(target[1:])] = _floats(rest[1:], where)
    elif key in ("PIM", "THC", "CCY", "RSL"):
        # 每个位置的像距求解开关与优化控制；位置的像距已由 ZOO THI 写明
        data["not_converted"].append(" ".join(cmd))
    else:
        data["unrecognized"].append(" ".join(cmd))


def read(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="latin-1")
    return parse(text, path.stem)


def problems(data: dict[str, Any]) -> list[str]:
    """不能转换的原因（与材料无关）：缺必需的系统数据、不支持的面或孔径。"""
    out = list(data["unsupported"])
    if not data["radius_mode"]:
        out.append("没有 RDM：面数据可能是曲率，不按半径解释")
    if data["object"] is None or data["image"] is None or not data["surfaces"]:
        out.append("缺 SO、S 或 SI 面")
    if data["units"] is None:
        out.append("缺 DIM 单位")
    if not data["wavelengths_nm"] or data["reference"] is None or not 1 <= data["reference"] <= len(data["wavelengths_nm"]):
        out.append("波长或参考波长缺失")
    if data["aperture"] is None:
        out.append("缺系统孔径")
    elif data["aperture"]["type"] == "image_na":
        out.append("像方 NA 孔径（NA）在 OpticStudio 没有对应的孔径类型")
    if data["fields"] is None:
        out.append("缺视场")
    if data["object"] is not None and data["object"]["radius"] != 0:
        out.append("物面是曲面")
    for k, s in enumerate(data["surfaces"], start=1):
        if s["asphere"] is not None and any(s["asphere"].get(c, 0.0) for c in ("H", "J")):
            out.append(f"S{k} 有 r^18/r^20 非球面项，偶次非球面只到 r^16")
        if s["pim"] and k != len(data["surfaces"]):
            out.append(f"S{k} 的 PIM 不在最后一面")
    stops = [k for k, s in enumerate(data["surfaces"], start=1) if s["stop"]]
    if len(stops) != 1:
        out.append(f"光阑面数为 {len(stops)}")
    if data["zoom"] is not None:
        n = len(data["surfaces"])
        # THI 的 S0 是物距、S(n+1) 是像面离焦；CUY 只用于 S1…Sn
        bad = [k for k in data["zoom"]["thickness"] if not 0 <= k <= n + 1] + [k for k in data["zoom"]["curvature"] if not 1 <= k <= n]
        if bad:
            out.append(f"ZOO 引用了不存在的面 {bad}")
    return out


def geometry(data: dict[str, Any], position: int | None = None) -> dict[str, Any]:
    """某个变焦位置（1 起）的面、孔径与视场；定焦镜头 position 必须为 None。"""
    zoom = data["zoom"]
    if zoom is None:
        if position is not None:
            raise _bad(f"{data['name']} 不是变焦镜头，不能指定 position")
        return {"object": data["object"], "surfaces": data["surfaces"], "image": data["image"], "aperture": data["aperture"], "fields": data["fields"]}
    if isinstance(position, bool) or not isinstance(position, int) or not 1 <= position <= zoom["positions"]:
        raise _bad(f"{data['name']} 是 {zoom['positions']} 个位置的变焦镜头：position 取 1…{zoom['positions']}，取出的是该位置的定焦镜头")
    i = position - 1
    chain = [dict(data["object"]), *(dict(s) for s in data["surfaces"]), dict(data["image"])]
    for k, values in zoom["thickness"].items():
        chain[k]["thickness"] = values[i]
    surfaces = chain[1:-1]
    for k, values in zoom["curvature"].items():
        surfaces[k - 1]["radius"] = 0.0 if values[i] == 0 else 1 / values[i]
    aperture = data["aperture"]
    if zoom["aperture"] is not None:
        if aperture is None or zoom["aperture"]["type"] != aperture["type"]:
            raise _bad(f"{data['name']}: ZOO 的孔径类型与系统孔径不同")
        aperture = {"type": aperture["type"], "value": zoom["aperture"]["values"][i]}
    fields = data["fields"]
    if zoom["fields"] and fields is not None:
        fields = {**fields, "x": list(fields["x"]), "y": list(fields["y"])}
        for key, per_field in zoom["fields"].items():
            kind, axis = FIELD_KEYS[key]
            if kind != fields["type"]:
                raise _bad(f"{data['name']}: ZOO 的视场类型与系统视场不同")
            for f, values in per_field.items():
                fields[axis][f - 1] = values[i]
    return {"object": chain[0], "surfaces": surfaces, "image": chain[-1], "aperture": aperture, "fields": fields}


def _media(surfaces: list[dict[str, Any]], private: dict[str, Any]) -> list[tuple[dict[str, Any], float | None]]:
    """每面之后的介质与带符号的 nd（反射后为负，同 CODE V/OpticStudio 的厚度符号）；无 nd 时为 None。"""
    out, sign, n = [], 1.0, 1.0
    for s in surfaces:
        m = material(s["token"], private)
        if m["kind"] == "mirror":
            sign = -sign
        elif m["kind"] == "air":
            n = 1.0
        elif m["kind"] == "code":
            n = m["nd"]
        else:
            n = math.nan
        out.append((m, None if math.isnan(n) else sign * n))
    return out


def paraxial(data: dict[str, Any], position: int | None = None) -> dict[str, Any]:
    """用各介质的 nd 算近轴一阶量（长度单位同原文件）。只作入库预筛与检索，数值以 OpticStudio 复现为准。"""
    g = geometry(data, position)
    media = _media(g["surfaces"], data["private"])
    missing = sorted({m["raw"] for m, n in media if n is None})
    if missing:
        raise _bad(f"材料没有 nd：{missing}")
    surfaces = [Surface(math.inf if s["radius"] == 0 else s["radius"], s["thickness"], n) for s, (_, n) in zip(g["surfaces"], media)]
    a, b, c, d = system_matrix(surfaces)
    if c == 0:
        raise _bad("近轴无焦（系统光焦度为零）")
    efl = -1.0 / c
    bfl = -surfaces[-1].n_after * a / c
    last = g["surfaces"][-1]["thickness"]
    out: dict[str, Any] = {"efl": efl, "bfl": bfl, "last_thickness": last}
    mirrors = any(m["kind"] == "mirror" for m, _ in media)
    if not mirrors:
        out["total_track"] = sum(s["thickness"] for s in g["surfaces"]) + g["image"]["thickness"]
    aperture, fields = g["aperture"], g["fields"]
    if aperture["type"] == "entrance_pupil_diameter":
        out["f_number"] = abs(efl) / aperture["value"]
    elif aperture["type"] == "image_f_number":
        out["f_number"] = aperture["value"]
    peak = max(math.hypot(x, y) for x, y in zip(fields["x"], fields["y"]))
    if fields["type"] == "angle":
        out["half_field_deg"] = peak
    obj = g["object"]["thickness"]
    if fields["type"] == "paraxial_image_height":
        out["image_height"] = peak
        if obj >= INFINITE_OBJECT:
            out["half_field_deg"] = math.degrees(math.atan(peak / abs(efl)))
    elif fields["type"] == "object_height":
        out["object_height"] = peak
    if obj < INFINITE_OBJECT:
        # 物面到最后一面的矩阵 [[a, a·t0 + b], [c, c·t0 + d]]；再走约化距离 t1 后 B = 0 处成像，此时 A 是横向放大率
        t1 = -(a * obj + b) / (c * obj + d) if c * obj + d else math.inf
        out["image_distance"] = t1 * surfaces[-1].n_after
        out["magnification"] = a + t1 * c if math.isfinite(t1) else math.inf
    else:
        out["image_distance"] = bfl
    return out


def elements(data: dict[str, Any], position: int | None = None) -> dict[str, Any]:
    """片数、组数（空气隔开的组）与各片在空气中的光焦度符号；有反射面时只数片数。"""
    g = geometry(data, position)
    media = _media(g["surfaces"], data["private"])
    lenses, groups, signs, group = 0, 0, [], []
    mirrors = sum(m["kind"] == "mirror" for m, _ in media)
    for k, (m, n) in enumerate(media):
        if m["kind"] in ("air", "mirror"):
            if group:
                signs.append(group[0] if len(group) == 1 else "(" + "".join(group) + ")")
                groups += 1
                group = []
            continue
        lenses += 1
        if mirrors or n is None or k + 1 >= len(g["surfaces"]):
            group.append("?")
            continue
        r1, r2 = g["surfaces"][k]["radius"], g["surfaces"][k + 1]["radius"]
        c1, c2, t, n = (0 if r1 == 0 else 1 / r1), (0 if r2 == 0 else 1 / r2), g["surfaces"][k]["thickness"], abs(n)
        power = (n - 1) * (c1 - c2) + (n - 1) ** 2 * t * c1 * c2 / n
        group.append("+" if power > 0 else "-" if power < 0 else "0")
    if group:
        signs.append(group[0] if len(group) == 1 else "(" + "".join(group) + ")")
        groups += 1
    return {"elements": lenses, "groups": groups, "mirrors": mirrors, "signs": None if mirrors else "".join(signs)}


def _material_out(m: dict[str, Any], materials: dict[str, Any]) -> Any:
    if m["kind"] == "air":
        return ""
    if m.get("raw") in materials:
        return materials[m["raw"]]
    if m["kind"] == "mirror":
        return "MIRROR"
    if m["kind"] == "code":
        return {"model": {"nd": m["nd"], "vd": m["vd"]}}
    if m["kind"] == "catalog":
        return m["name"]
    return None


def to_prescription(data: dict[str, Any], *, position: int | None = None, materials: dict[str, Any] | None = None,
                    allow_unrecognized: bool = False) -> dict[str, Any]:
    """转成 lens build 处方（已 normalize）。materials 把原文件的材料名（引号内的名字或 NAME_SPECIAL）映射到 saos 材料。
    返回 {"prescription", "not_converted", "materials"}；not_converted 是认识但没带过去的命令（渐晕、优化控制等）。"""
    materials = dict(materials or {})
    for key, value in materials.items():
        materials[key] = rx.normalize_material(value, f"materials[{key!r}]")
    blockers = problems(data)
    if blockers:
        raise _bad(f"{data['name']} 不能转换：{'；'.join(blockers)}", problems=blockers)
    if data["unrecognized"] and not allow_unrecognized:
        raise _bad(f"{data['name']} 有不认识的命令 {data['unrecognized']}；确认含义后可用 allow_unrecognized=True 跳过", unrecognized=data["unrecognized"])
    g = geometry(data, position)
    surfaces, used, missing = [], {}, []
    for k, s in enumerate(g["surfaces"], start=1):
        m = material(s["token"], data["private"])
        out_material = _material_out(m, materials)
        if out_material is None:
            missing.append(f"S{k} {m['raw']}" + ("（PRV 私有玻璃，折射率表见索引）" if m["kind"] == "private" and m.get("defined") else ""))
            continue
        if m["kind"] != "air":
            used[m["raw"]] = out_material
        item: dict[str, Any] = {"radius": math.inf if s["radius"] == 0 else s["radius"], "thickness": s["thickness"],
                                "material": out_material, "stop": s["stop"]}
        if s["asphere"] is not None:
            item.update(type="EvenAspheric", conic=s["conic"], params={f"par{ASPHERE[c]}": v for c, v in s["asphere"].items() if v})
        elif s["conic"]:
            item["conic"] = s["conic"]
        if s["pim"] and data["afocal_image"] is None:
            # PIM：近轴像距求解，即像面上近轴边缘光线高度为零；无焦像空间没有近轴像，保留原厚度
            item["solves"] = {"thickness": {"type": "marginal_ray_height", "height": 0.0, "zone": 0.0}}
        surfaces.append(item)
    if missing:
        raise _bad(f"{data['name']} 有材料需要在 materials 里指定（不自动替换）：{missing}", materials=missing)
    image = g["image"]
    if image["thickness"]:
        # SI 的厚度是相对近轴像面的离焦，与 OpticStudio 自带转换器一样用一个虚设面表示
        surfaces.append({"radius": math.inf, "thickness": image["thickness"], "comment": "CODE V SI thickness (defocus)"})
    vendors = sorted({m["vendor"] for s in g["surfaces"] for m in [material(s["token"], data["private"])] if m["kind"] == "catalog"})
    fields = g["fields"]
    p = {"title": data["title"], "units": data["units"],
         "aperture": g["aperture"],
         "fields": {"type": fields["type"], "x": fields["x"], "y": fields["y"],
                    **({"weights": data["field_weights"]} if data["field_weights"] and len(data["field_weights"]) == len(fields["y"]) else {})},
         "wavelengths": {"um": [w / 1000 for w in data["wavelengths_nm"]], "primary": data["reference"],
                         **({"weights": data["wave_weights"]} if data["wave_weights"] and len(data["wave_weights"]) == len(data["wavelengths_nm"]) else {})},
         "catalogs": vendors or ["SCHOTT"],
         "object": {"thickness": "inf" if g["object"]["thickness"] >= INFINITE_OBJECT else g["object"]["thickness"]},
         "surfaces": surfaces,
         "image": {"radius": math.inf if image["radius"] == 0 else image["radius"]}}
    if data["afocal_image"] is not None:
        p["afocal"] = True
    not_converted = list(data["not_converted"])
    if data["vignetting"] or (data["zoom"] and data["zoom"]["vignetting"]):
        not_converted.append("渐晕系数 VUX/VLX/VUY/VLY（CODE V 与 OpticStudio 的定义不同，未换算）")
    if data["afocal_image"] is not None:
        not_converted.append(f"AFI {data['afocal_image']}：转成无焦像空间，没有加理想透镜")
    return {"prescription": rx.normalize(p), "not_converted": not_converted, "materials": used,
            "unrecognized": list(data["unrecognized"]), "position": position}
