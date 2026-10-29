"""rtc 实例（SAOS_BACKEND=rtc）的镜头：处方 → rtc 镜头，rtc 镜头 → .zmx，以及 OpticStudio 会替用户重算的量
（边缘光线高度求解、自动半口径）。
Lenses of the rtc instance: prescription → rtc lens, rtc lens → .zmx, and what OpticStudio recomputes for the user
(marginal-ray-height solves, automatic semi-diameters).

.zmx 只写在 OpticStudio 24.1 存盘文件里见过的行（rtc 的 fixtures/lenses 与 tests/fixtures/cases/*.zmx）；写法没见过的
处方项（拾取、F 数求解、膜层、矩形与椭圆孔径、偏心倾斜）报 BACKEND_UNSUPPORTED，不猜格式。
Only lines seen in files OpticStudio 24.1 saved are written; prescription items whose file form was never seen are
refused with BACKEND_UNSUPPORTED instead of guessing a format.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from saos.protocol import ErrorCode, SaosError

# 处方面型（小写去分隔符）→ .zmx 的 TYPE / prescription surface type → .zmx TYPE
TYPES = {"standard": "STANDARD", "paraxial": "PARAXIAL", "evenaspheric": "EVENASPH", "evenasphere": "EVENASPH",
         "biconic": "BICONICX", "toroidal": "TOROIDAL"}
APERTURES = {"entrance_pupil_diameter": "EPD", "object_na": "OBNA"}
FIELD_TYPES = {"angle": ("ANGLE", 0), "object_height": ("OP1", 1)}
AIMING = {"off": 0, "paraxial": 1, "real": 2}
# 自动半口径：每个视场、每个波长沿光瞳边缘取这么多条光线 / rim rays per field and wavelength for automatic semi-diameters
RIM_RAYS = 16
# UTF-16LE 的字节序标记 / the UTF-16LE byte order mark
BOM = bytes((0xFF, 0xFE))


def unsupported(message: str, problems: list[str] | None = None) -> SaosError:
    return SaosError(ErrorCode.BACKEND_UNSUPPORTED, message, {"problems": problems} if problems else None)


def _type_key(name: str) -> str:
    return name.replace(" ", "").replace("_", "").replace("-", "").lower()


def _material(value: Any):
    from rtc.material.types import Catalog, Model

    if value in (None, ""):
        return None
    if isinstance(value, dict):
        m = value["model"]
        return Model(float(m["nd"]), float(m["vd"]), float(m.get("dpgf", 0.0)))
    if str(value).upper() == "MIRROR":
        raise unsupported("rtc does not trace mirrors; use an OpticStudio instance for reflective systems")
    return Catalog(str(value).upper())


def from_prescription(p: dict[str, Any]):
    """规范化处方（saos.prescription.normalize）→ rtc.zmx.Lens；rtc 复现不了或 .zmx 写不出的项一次全部列出。
    A normalized prescription → rtc.zmx.Lens; everything rtc cannot reproduce or write is listed at once."""
    from rtc import zmx
    from rtc.material.types import Environment

    if p.get("mode") == "non_sequential":
        raise unsupported("rtc traces sequential systems only; use an OpticStudio instance for non-sequential systems")
    problems: list[str] = []
    if p["units"] != "mm":
        problems.append(f"units {p['units']} (rtc works in mm)")
    aperture = APERTURES.get(p["aperture"]["type"])
    if aperture is None:
        problems.append(f"aperture {p['aperture']['type']} (rtc has entrance_pupil_diameter and object_na)")
    field = FIELD_TYPES.get(p["fields"]["type"])
    if field is None:
        problems.append(f"fields.type {p['fields']['type']} (rtc has angle and object_height)")
    for key in ("afocal", "apodization", "polarization", "coating_file", "ray_aiming_options"):
        if p.get(key):
            problems.append(f"{key} (not reproduced or written by rtc)")
    if p.get("telecentric_object"):
        problems.append("telecentric_object")
    if p.get("nsc"):
        problems.append("nsc settings")
    if p["object"]["material"]:
        problems.append("object space material (rtc assumes air in object space)")
    for row in p.get("configurations") or []:
        if row["operand"] != "THIC" or row.get("vary"):
            problems.append(f"configuration operand {row['operand']}{' (vary)' if row.get('vary') else ''} (rtc has fixed THIC rows)")

    lens = zmx.Lens(path="", name=p.get("title", ""), units="MM")
    lens.raw["MODE"] = "MODE SEQ"
    lens.raw["VERS"] = "VERS 231205 0 20120530 20120530"
    lens.raw["GCAT"] = "GCAT " + " ".join(p["catalogs"]) + " "
    env = p["environment"]
    lens.environment = Environment(float(env["temperature"]), float(env["pressure"]), bool(env["adjust_index"]))
    lens.raw["ENVD"] = f"ENVD {env['temperature']:.12g} {env['pressure']:.12g} {1 if env['adjust_index'] else 0}"
    lens.aperture_type, lens.aperture_value = aperture or "EPD", float(p["aperture"]["value"] or 0.0)
    f = p["fields"]
    lens.field_type = field[0] if field else "ANGLE"
    lens.field_normalization = f["normalization"]
    lens.fields = [(float(x), float(y), float(w)) for x, y, w in zip(f["x"], f["y"], f["weights"])]
    lens.num_fields = len(lens.fields)
    w = p["wavelengths"]
    lens.wavelengths = [(float(um), float(wt)) for um, wt in zip(w["um"], w["weights"])]
    lens.num_waves = len(lens.wavelengths)
    lens.primary_wave = int(w["primary"])
    lens.ray_aiming = AIMING[p["ray_aiming"]]

    obj = zmx.Surface(index=0, thickness=float(p["object"]["thickness"]), comment=p["object"]["comment"])
    lens.surfaces.append(obj)
    rows = list(p["surfaces"]) + [{**(p.get("image") or {"type": "standard", "radius": math.inf}), "image": True}]
    for k, s in enumerate(rows, start=1):
        try:
            lens.surfaces.append(_surface(k, s))
        except SaosError as exc:
            problems.append(exc.message)
    if problems:
        raise unsupported("rtc cannot build this prescription: " + "; ".join(problems), problems)
    configs = p.get("configurations") or []
    if configs:
        lens.num_configs = len(configs[0]["values"])
        for row in configs:
            surface = int(row["params"][0]) if row["params"] else 0
            for c, value in enumerate(row["values"], start=1):
                lens.mce.append(zmx.MCEOperand(op="THIC", surface=surface, config=c, value=float(value)))
    return lens


def _surface(k: int, s: dict[str, Any]):
    from rtc import zmx

    where = f"surface {k}" if not s.get("image") else "image"
    kind = TYPES.get(_type_key(s["type"]))
    if kind is None:
        raise unsupported(f"{where} type {s['type']} (rtc builds {sorted(set(TYPES.values()))})")
    for key in ("decenter_tilt", "coating", "objects"):
        if s.get(key):
            raise unsupported(f"{where} {key}")
    out = zmx.Surface(index=k, type=kind, comment=s.get("comment", ""))
    radius = s.get("radius", math.inf)
    out.curvature = 0.0 if math.isinf(radius) or radius == 0 else 1.0 / float(radius)
    out.conic = float(s.get("conic", 0.0))
    out.thickness = float(s.get("thickness", 0.0))
    out.is_stop = bool(s.get("stop"))
    if "material" in s:
        out.material = _material(s["material"])
    if kind == "PARAXIAL":
        out.params[1] = float(s["focal_length"])
        out.params[2] = float(s.get("opd_mode", 1))
    for key, value in (s.get("params") or {}).items():
        if not str(key).lower().startswith("par") or not str(key)[3:].isdigit():
            raise unsupported(f"{where} parameter {key!r}: give rtc surface parameters by Par column number (par<N>)")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise unsupported(f"{where} {key}: rtc takes numeric surface parameters only")
        out.params[int(str(key)[3:])] = float(value)
    if s.get("semi_diameter") is not None:
        out.semi_diameter, out.semi_fixed = float(s["semi_diameter"]), True
    if s.get("mech_semi_diameter") is not None:
        out.mech_semi_diameter, out.mech_fixed = float(s["mech_semi_diameter"]), True
    ap = s.get("aperture")
    if ap:
        if ap["type"] != "circular" or ap.get("x_decenter") or ap.get("y_decenter"):
            raise unsupported(f"{where} aperture {ap['type']} (rtc writes centred circular apertures only)")
        out.aperture_code, out.ap_min, out.ap_max = 1, float(ap["min"]), float(ap["max"])
    for column, solve in (s.get("solves") or {}).items():
        if solve["type"] == "variable" and column == "radius":
            out.solve_radius = 1
        elif solve["type"] == "variable" and column == "conic":
            out.raw["VCON"] = "VCON"
        elif solve["type"] == "variable" and column == "thickness":
            out.raw["VDSZ"] = "VDSZ 0 0"
        elif solve["type"] == "variable" and column.startswith("par"):
            out.variable_params.add(int(column[3:]))
        elif solve["type"] == "marginal_ray_height" and float(solve.get("zone", 0.0)) == 0.0:
            out.raw["MAZH"] = f"MAZH {float(solve['height']):.17g} 0"
        else:
            raise unsupported(f"{where} {column} solve {solve['type']} (rtc recomputes paraxial marginal ray height solves only)")
    return out


# ---- OpticStudio 会重算的量 / what OpticStudio recomputes ----

def marginal_height_solves(lens) -> list[tuple[int, float]]:
    """镜头里的边缘光线高度求解：[(面号, 目标高度)]，只认瞳带 0（近轴边缘光线）。"""
    out = []
    for s in lens.surfaces:
        raw = s.raw.get("MAZH")
        if raw:
            parts = raw.split()
            if len(parts) > 2 and float(parts[2]) != 0.0:
                raise unsupported(f"surface {s.index} marginal ray height solve at pupil zone {parts[2]} (rtc solves zone 0 only)")
            out.append((s.index, float(parts[1])))
    return out


def paraxial_marginal(system) -> list[tuple[float, float, float]]:
    """主波长的近轴边缘光线逐面 (y, u_前, u_后)；物在无穷远时从入瞳边缘平行入射，有限物距时从物面中心射向入瞳边缘。
    The primary-wavelength paraxial marginal ray per surface: parallel at the pupil edge, or from the object centre."""
    r = float(system.pupil_radius())
    if not system.finite_object:
        return system._paraxial(r, 0.0)
    z_ep, _ = system.entrance_pupil()
    u = r / (z_ep + system.object_thickness)
    return system._paraxial(u * system.object_thickness, u)


def solve_thickness(rows, k: int, height: float) -> float | None:
    """第 k 项（items 下标）之后的厚度，使近轴边缘光线在下一面的高度为 height；光线与轴平行时无解。"""
    y, _, u = rows[k]
    return None if u == 0.0 else (height - y) / u


def auto_semi_diameters(system, lens, config: int, waves: int, trace) -> dict[int, float]:
    """自动半口径：各视场、各波长沿光瞳边缘的光线在每面的最大径向高度（OpticStudio 自动半口径的口径定义，未与它对账）。
    trace(wave, hx, hy, px, py) → rtc RayBlock。
    Automatic semi-diameters: the largest radial height of the pupil-rim rays of every field and wavelength."""
    sx, sy = system._field_scale()
    angles = np.linspace(0.0, 2.0 * np.pi, RIM_RAYS, endpoint=False)
    px, py = np.cos(angles), np.sin(angles)
    best: dict[int, float] = {}
    for x, y, _ in lens.fields:
        hx = x / sx if sx else 0.0
        hy = y / sy if sy else 0.0
        for w in range(1, waves + 1):
            block = trace(w, np.full(RIM_RAYS, hx), np.full(RIM_RAYS, hy), px, py)
            ok = block.error == 0
            for k, index in enumerate(block.surface_index):
                if not ok.any():
                    continue
                r = float(np.nanmax(np.hypot(block.x[ok, k], block.y[ok, k])))
                if math.isfinite(r):
                    best[index] = max(best.get(index, 0.0), r)
    return best


# ---- .zmx ----

# dump 自己生成的文件头行；其余文件头行（偏振 POLS、膜层文件 COFN、玻璃替换模板 GSTD…）rtc 不建模，从读入的文件原样带过去
# header lines dump writes itself; the others (POLS polarization, COFN coating files, GSTD, …) are not modelled by rtc and
# are carried over verbatim from the file that was read
GENERATED_HEADER = frozenset({"VERS", "MODE", "NAME", "UNIT", "ENPD", "OBNA", "ENVD", "GCAT", "RAIM", "FTYP", "XFLN", "YFLN",
                              "FWGN", "VDXN", "VDYN", "VCXN", "VCYN", "VANN", "WAVM", "PWAV", "XFLD", "YFLD"})
# 面块里 rtc 不建模、但 OpticStudio 存盘总会写的显示类行：原样带过去 / display lines OpticStudio always saves: carried over
SURFACE_PASSTHROUGH = ("HIDE", "MIRR", "SLAB", "POPS", "FIMP", "SSID")
# 面块里 rtc 建模并自己写出的行 / surface lines rtc models and writes itself
SURFACE_MODELLED = frozenset({"VDSZ", "VCON", "MAZH", "CLAP", "FLAP"})


def carried_lines(text: str) -> dict[str, list[str]]:
    """读入的 .zmx 里 dump 不生成、要原样带过去的行：第一个 SURF 之前的文件头行，和公差行（TOL，在评价函数之后）。
    Lines of a read .zmx that dump does not generate and carries over: header lines before the first SURF and TOL rows."""
    header, tail = [], []
    seen_surface = False
    for line in text.lstrip("﻿").splitlines():
        if not line.strip() or line[:1].isspace():
            continue
        key = line.split()[0].upper()
        if key == "SURF":
            seen_surface = True
        elif not seen_surface and key not in GENERATED_HEADER:
            header.append(line.strip())
        elif seen_surface and key == "TOL":
            tail.append(line.strip())
    return {"header": header, "tail": tail}


def unknown_surface_lines(lens) -> list[str]:
    """面块里 rtc 没见过、也不建模的行（拾取等求解、膜层…）：照算或丢掉都会让存盘的镜头与 rtc 算的不同，交给调用方拒绝。
    Surface lines rtc has neither seen nor models (pickups and other solves, coatings, …); the caller refuses them."""
    known = SURFACE_MODELLED | set(SURFACE_PASSTHROUGH)
    return [f"surface {s.index}: {s.raw[key]}" for s in lens.surfaces for key in s.raw if key not in known]


def _num(v: float) -> str:
    if math.isinf(v):
        return "INFINITY" if v > 0 else "-INFINITY"
    return repr(float(v))


def dump(lens, title: str | None = None, carried: dict[str, list[str]] | None = None) -> str:
    """rtc 镜头 → .zmx 文本（OpticStudio 24.1 存盘的行格式）。只写 rtc 镜头模型里的量和 carried（carried_lines 的结果）里原样
    带过去的行，其余行由 OpticStudio 打开时取默认值。
    An rtc lens → .zmx text in the line format OpticStudio 24.1 saves: what the rtc lens model holds plus the lines in
    carried (from carried_lines); OpticStudio takes defaults for the rest."""
    from rtc.material.types import Catalog, Model

    lines = ["VERS 231205 0 20120530 20120530", "MODE SEQ", f"NAME {title if title is not None else lens.name}", "UNIT MM X W X CM MR CPMM"]
    if lens.aperture_type == "OBNA":
        lines.append(f"OBNA {_num(lens.aperture_value)} 0")
    else:
        lines.append(f"ENPD {_num(lens.aperture_value)}")
    env = lens.environment
    lines.append(f"ENVD {env.temperature:.12g} {env.pressure:.12g} {1 if env.adjust else 0}")
    lines.append("GCAT " + " ".join(lens.catalogs) + " ")
    lines.append(f"RAIM 0 {int(lens.ray_aiming)} 1 1 0 0 0 0 0 1")
    nf, nw = len(lens.fields), len(lens.active_wavelengths)
    ftype = 1 if lens.field_type == "OP1" else 0
    norm = 1 if lens.field_normalization == "rectangular" else 0
    lines.append(f"FTYP {ftype} 0 {nf} {nw} {norm} 0 0 {nf}")
    lines.append("XFLN " + " ".join(_num(x) for x, _, _ in lens.fields))
    lines.append("YFLN " + " ".join(_num(y) for _, y, _ in lens.fields))
    lines.append("FWGN " + " ".join(_num(w) for _, _, w in lens.fields))
    for key in ("VDXN", "VDYN", "VCXN", "VCYN", "VANN"):
        lines.append(f"{key} " + " ".join("0" for _ in lens.fields))
    for k, (um, weight) in enumerate(lens.active_wavelengths, start=1):
        lines.append(f"WAVM {k} {_num(um)} {_num(weight)}")
    lines.append(f"PWAV {int(lens.primary_wave)}")
    lines.extend((carried or {}).get("header", []))
    for s in sorted(lens.surfaces, key=lambda s: s.index):
        lines.append(f"SURF {s.index}")
        if s.is_stop:
            lines.append("  STOP")
        lines.append(f"  TYPE {s.type}")
        if s.comment:
            lines.append(f"  COMM {s.comment}")
        lines.append(f"  CURV {_num(s.curvature)} {1 if s.solve_radius == 1 else 0} 0 0 0 \"\"")
        for n in sorted(s.params):
            if n > 12 and n - 12 in s.extra:
                continue
            lines.append(f"  PARM {n} {_num(s.params[n])}")
            if n in s.variable_params:
                lines.append(f"  VPAR {n}")
        lines.append(f"  DISZ {_num(s.thickness)}")
        if "VDSZ" in s.raw:
            # 厚度变量：OpticStudio 存盘写成单独的 VDSZ 行（rtc fixtures/rounds 里所见）/ a thickness variable, saved as its own VDSZ line
            lines.append("  VDSZ 0 0")
        if "MAZH" in s.raw:
            lines.append(f"  {s.raw['MAZH']}")
        spec = s.material
        if isinstance(spec, Model):
            lines.append(f"  GLAS ___BLANK 1 0 {_num(spec.nd)} {_num(spec.vd)} {_num(spec.dpgf)} 0 0 0 0 0 ")
        elif isinstance(spec, Catalog):
            lines.append(f"  GLAS {spec.name} 0 0 1.5 40 0 0 0 0 0 0 ")
        if s.conic:
            lines.append(f"  CONI {_num(s.conic)}")
        if "VCON" in s.raw:
            lines.append("  VCON")
        lines.append(f"  DIAM {_num(s.semi_diameter)} {1 if s.semi_fixed else 0} 0 0 1 \"\"")
        lines.append(f"  MEMA {_num(s.mech_semi_diameter)} {1 if s.mech_fixed else 0} 0 0 1 \"\"")
        if s.aperture_code == 1:
            lines.append(f"  CLAP {_num(s.ap_min)} {_num(s.ap_max)} 0")
        elif s.aperture_code == 10:
            lines.append(f"  FLAP {_num(s.ap_min)} {_num(s.ap_max)} 0")
        elif s.aperture_code:
            raise unsupported(f"surface {s.index} aperture code {s.aperture_code} cannot be written by rtc")
        lines.extend(f"  {s.raw[key]}" for key in SURFACE_PASSTHROUGH if key in s.raw)
    # 评价函数行在面之后、多重结构之前（OpticStudio 存盘的顺序；放在 MNUM 之后会被读成多重结构行）
    # merit rows follow the surfaces and precede the multi-configuration block, as OpticStudio saves them
    lines.extend(row.raw for row in lens.mfe)
    # 公差行在评价函数之后、MNUM 之前（同 OpticStudio 存盘的顺序）/ tolerance rows sit between the merit rows and MNUM
    lines.extend((carried or {}).get("tail", []))
    if lens.mce:
        lines.append(f"MNUM {lens.num_configs} {lens.current_config}")
        for row in lens.mce:
            if row.op not in ("THIC", "MOFF") or (row.op == "THIC" and row.pickup is not None):
                raise unsupported(f"multi-configuration row {row.op} cannot be written by rtc")
            if row.raw:
                # rtc 不改多重结构的格，读入的行原样写回 / rtc never changes configuration cells: rows read are written back as they were
                lines.append(row.raw)
            elif row.op == "THIC":
                lines.append(f"THIC {row.surface:3d} {row.config:3d} {row.value:.12E} 0 0 0 1 1 1.000000000000E+00 0.000000000000E+00 0 \"\" 0")
            else:
                raise unsupported("a MOFF row without its file line cannot be written by rtc")
    return "\r\n".join(lines) + "\r\n"


def write(lens, path, title: str | None = None, carried: dict[str, list[str]] | None = None) -> None:
    """按 OpticStudio 的存盘编码（UTF-16LE + BOM）写出。"""
    path.write_bytes(BOM + dump(lens, title, carried).encode("utf-16-le"))
