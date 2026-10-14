"""把专利（或教材）提取出的面数据适配成 saos 标准设计布局的处方，并给出复现校验的期望值。

提取格式见 docs/library.md（schema 2）；第 3 步的旧格式（schema 1）在读取时自动转换。
"""

from __future__ import annotations

import math
from typing import Any

from saos import glasscat, prescription
from saos.recipes.microscope.paraxial import Surface, back_focal_distance, effective_focal_length, front_focal_distance
from saos.recipes.microscope import layout
from saos.protocol import ErrorCode, SaosError
from saos.recipes.microscope.spec import REFERENCE_TUBE_MM

WAVES = {
    "d": [0.4861327, 0.5875618, 0.6562725],  # F、d、C，主波长 d
    "e": [0.4799914, 0.5460740, 0.6438469],  # F′、e、C′，主波长 e
}
MODEL_GLASS_DISTANCE = 3.0
NAME_ND_TOLERANCE = 0.002
# 原文印出的折射率不超过 3 位小数时，目录中 ±半个末位内常有多种玻璃，按 nd/νd 选最近牌号没有意义，改用模型玻璃
MODEL_GLASS_MAX_ND_DECIMALS = 3
# 原文说明工作距离从盖玻片量起、却没有给厚度时的假设：ISO 8255-1 #1.5H，0.17 mm；
# d 线折射率与阿贝数按 D263 类盖玻片近似取 1.523、55（ISO 规定的是 n_e = 1.5255、ν_e = 56）
DEFAULT_COVER = {"thickness": 0.17, "n": 1.523, "nu": 55.0}
ENVELOPE_SAMPLES = 4000


def _num(value: Any) -> float:
    if value is None or (isinstance(value, str) and value.strip().lower() in ("inf", "infinity", "∞", "")):
        return math.inf
    return float(value)


def _bad(message: str) -> SaosError:
    return SaosError(ErrorCode.BAD_ARGS, f"适配失败：{message}")


def upgrade(extract: dict[str, Any], embodiment: str | None = None) -> dict[str, Any]:
    """把 schema 1（{"embodiment_1": {...}, "patent": ...}）转换为 schema 2。"""
    if extract.get("schema") == 2:
        return extract
    key = embodiment or "embodiment_1"
    emb = extract.get(key)
    if not isinstance(emb, dict):
        raise _bad(f"提取结果中没有 {key}")
    header = dict(emb["header"])
    beta = header.get("beta")
    if isinstance(beta, str):
        header["beta"] = float(beta.lower().replace("x", "").replace("×", ""))
    return {
        "schema": 2,
        "source": {"patent": extract.get("patent")},
        "embodiment": key,
        "units": "normalized" if float(header.get("f", 0)) == 1 else "mm",
        "order": "object_to_image",
        "wavelength": None,
        "header": header,
        "cover_glass": None,
        "claimed_class": None,
        "surfaces": emb["surfaces"],
        "doubts": extract.get("doubts", []),
    }


def oriented_rows(ex: dict[str, Any]) -> list[dict[str, Any]]:
    """返回从物方到像方排列的面。若原表从像方列起，则倒序：半径变号，介质与间隔顺移一面。"""
    rows = [dict(r) for r in ex["surfaces"]]
    for r in rows:
        if r.get("aspheric"):
            raise _bad(f"面 {r.get('i')} 是非球面，第一版只收全球面结构")
    if ex.get("order", "object_to_image") == "object_to_image":
        return rows
    if ex["order"] != "image_to_object":
        raise _bad(f"order 只能是 object_to_image / image_to_object，收到 {ex['order']}")
    flipped = []
    n = len(rows)
    for k in range(n):
        src = rows[n - 1 - k]
        nxt = rows[n - 2 - k] if n - 2 - k >= 0 else None  # 原表中位于它之前的面
        row = {"i": src.get("i"), "r": -_num(src["r"]) if not math.isinf(_num(src["r"])) else math.inf, "stop": bool(src.get("stop"))}
        if nxt is not None:
            row["d"] = nxt.get("d")
            for key in ("n", "nu", "glass"):
                if key in nxt:
                    row[key] = nxt[key]
        flipped.append(row)
    return flipped


def _decimals(value: Any) -> int:
    if value is None:
        return 0
    text = repr(float(value)) if not isinstance(value, str) else value
    return len(text.split(".")[1]) if "." in text and "e" not in text.lower() else 0


def column_decimals(rows: list[dict[str, Any]]) -> dict[str, int]:
    """每列印刷的小数位数：JSON 数值会丢掉末尾的 0（42.0030 读成 42.003），所以取同一列中的最大位数。"""
    out = {}
    for key in ("r", "d", "n", "nu"):
        values = [r.get(key) for r in rows if isinstance(r.get(key), (int, float))]
        out[key] = max((_decimals(v) for v in values), default=0)
    return out


def rounding_envelope(rows: list[dict[str, Any]], cover: dict[str, Any] | None, *, seed: int = 0) -> dict[str, list[float]]:
    """把每个印刷值在 ±半个末位内均匀扰动，求焦距和工作距离可能的范围（近轴、原文单位）。"""
    import random

    dec = column_decimals(rows)
    rng = random.Random(seed)
    f_vals, wd_vals = [], []
    for _ in range(ENVELOPE_SAMPLES):
        lens = []
        for r in rows:
            def jitter(key: str, value: float) -> float:
                return value + rng.uniform(-0.5, 0.5) * 10 ** -dec[key] if math.isfinite(value) else value

            rr = jitter("r", _num(r["r"]))
            dd = jitter("d", _num(r.get("d") or 0.0)) if r.get("d") is not None else 0.0
            nn = jitter("n", float(r["n"])) if "n" in r else 1.0
            lens.append(Surface(rr, dd, nn))
        f_vals.append(effective_focal_length(lens))
        wd = front_focal_distance(lens)
        if cover:
            wd -= float(cover["thickness"]) / float(cover["n"])
        wd_vals.append(wd)
    return {"efl": [min(f_vals), max(f_vals)], "wd": [min(wd_vals), max(wd_vals)]}


def match_glasses(rows: list[dict[str, Any]], catalogs: list[str]) -> dict[tuple[float, float, str], dict[str, Any]]:
    """每种玻璃：印了牌号且在目录中、nd 相符时直接用；否则按 nd/νd 取最近牌号；太远时用模型玻璃。"""
    library = glasscat.load_catalogs(catalogs)
    by_name = {}
    for g in library:
        by_name.setdefault(g.name.upper(), g)
    result: dict[tuple[float, float, str], dict[str, Any]] = {}
    for row in rows:
        if "n" not in row:
            continue
        name = str(row.get("glass") or "").strip().upper()
        key = (float(row["n"]), float(row["nu"]), name)
        if key in result:
            continue
        hits = glasscat.nearest(key[0], key[1], library, k=3)
        candidates = [{"catalog": g.catalog, "name": g.name, "nd": g.nd, "vd": g.vd, "status": g.status, "distance": round(s, 2)} for s, g in hits]
        printed = by_name.get(name)
        if not name and column_decimals(rows)["n"] <= MODEL_GLASS_MAX_ND_DECIMALS:
            result[key] = {
                "material": {"model": {"nd": key[0], "vd": key[1], "dpgf": 0.0}},
                "catalog": None,
                "status": "Model",
                "how": f"原文折射率只印 {column_decimals(rows)['n']} 位小数，用模型玻璃",
                "candidates": candidates,
            }
            continue
        if printed is not None and abs(printed.nd - key[0]) <= NAME_ND_TOLERANCE:
            result[key] = {"material": printed.name, "catalog": printed.catalog, "status": printed.status, "how": "printed name", "candidates": candidates}
            continue
        distance, best = hits[0]
        if distance <= MODEL_GLASS_DISTANCE:
            result[key] = {"material": best.name, "catalog": best.catalog, "status": best.status, "how": f"nd/νd 最近（距离 {distance:.2f}）", "candidates": candidates}
        else:
            result[key] = {"material": {"model": {"nd": key[0], "vd": key[1], "dpgf": 0.0}}, "catalog": None, "status": "Model", "how": "模型玻璃", "candidates": candidates}
    return result


def _glass_key(r: dict[str, Any]) -> tuple[float, float, str]:
    return (float(r["n"]), float(r["nu"]), str(r.get("glass") or "").strip().upper())


def native_focal_length(ex: dict[str, Any]) -> float:
    """库中保存的焦距：毫米制按原值；归一化数据按 β 换算到 200 mm 参考筒镜下的焦距。"""
    header = ex["header"]
    if ex.get("units") == "mm":
        return float(header["f"])
    beta = header.get("beta")
    if beta is None:
        raise _bad("归一化数据需要表头的 β 才能确定焦距")
    tube = float(header.get("tube_lens_f") or REFERENCE_TUBE_MM)
    # 专利的 β 对应它自己的筒镜焦距；换到参考筒镜时保持 f 不变，即 f = tube / |β|
    return tube / abs(float(beta))


def resolve_cover_glass(cover: dict[str, Any] | None, *, scale: float) -> tuple[dict[str, Any] | None, list[str]]:
    """补齐盖玻片，返回原文长度单位下的参数及缺省字段的说明。"""
    if not cover:
        return None, []
    resolved = dict(cover)
    assumptions = []
    for key, default in DEFAULT_COVER.items():
        if cover.get(key) is not None:
            continue
        # 后续处方、库指标和舍入包络都统一缩放，毫米制默认厚度需先换回原文单位。
        resolved[key] = default / scale if key == "thickness" else default
        label = {"thickness": "厚度 0.17 mm", "n": "nd 1.523", "nu": "νd 55"}[key]
        assumptions.append(f"盖玻片原文缺少 {key}，按 {label} 假设")
    return resolved, assumptions


def from_extraction(
    extract: dict[str, Any],
    *,
    catalogs: list[str],
    embodiment: str | None = None,
    focal_length_mm: float | None = None,
    tube_lens_mm: float = REFERENCE_TUBE_MM,
    field_number: float | None = None,
    stop: str | None = None,
) -> dict[str, Any]:
    ex = upgrade(extract, embodiment)
    header = ex["header"]
    rows = oriented_rows(ex)
    f_target = focal_length_mm or native_focal_length(ex)
    scale = f_target / float(header["f"])
    na = float(header["NA"])
    wave = ex.get("wavelength") or "d"
    if wave not in WAVES:
        raise _bad(f"wavelength 只能是 d / e，收到 {wave}")

    printed_stop = [k for k, r in enumerate(rows) if r.get("stop")]
    stop = stop or ("printed" if printed_stop else "rear_focal")

    lens = [Surface(_num(r["r"]), _num(r.get("d") or 0.0), float(r.get("n", 1.0))) for r in rows]
    efl = effective_focal_length(lens) * scale
    ffd = front_focal_distance(lens) * scale
    bfd = back_focal_distance(lens) * scale
    glasses = match_glasses(rows, catalogs)

    surfaces: list[dict[str, Any]] = []
    for k, r in enumerate(rows):
        material = glasses[_glass_key(r)]["material"] if "n" in r else ""
        last = k == len(rows) - 1
        thickness = bfd if (last and stop == "rear_focal") else (0.0 if last else _num(r["d"]) * scale)
        is_stop = (stop == "printed" and r.get("stop")) or stop == f"r{r.get('i')}"
        surfaces.append(
            {
                "radius": _num(r["r"]) * scale,
                "thickness": thickness,
                "material": material,
                "comment": layout.TAG_STOP if (r.get("stop") and math.isinf(_num(r["r"]))) else f"patent r{r.get('i')}",
                "stop": bool(is_stop),
            }
        )
    if stop == "rear_focal":
        # 物镜后焦面上的虚拟光阑：物方远心。参考筒镜放在同一平面，位置不影响理想透镜成像
        surfaces.append({"radius": math.inf, "thickness": 0.0, "comment": layout.TAG_STOP, "stop": True})
    elif not any(s["stop"] for s in surfaces):
        raise _bad(f"stop 必须是 rear_focal、printed 或 r<面号>，收到 {stop}")
    surfaces.append({"type": "paraxial", "focal_length": tube_lens_mm, "opd_mode": 1, "thickness": tube_lens_mm, "comment": layout.TAG_TUBE})

    cover, cover_assumptions = resolve_cover_glass(ex.get("cover_glass"), scale=scale)
    if cover:
        t_c, n_c = float(cover["thickness"]) * scale, float(cover["n"])
        # 平行平板使物点的近轴像前移 t(1 - 1/n)，所以盖玻片上表面到第一面的间隔取 前焦距 - t/n
        gap = ffd - t_c / n_c
        obj = {"thickness": t_c, "material": {"model": {"nd": n_c, "vd": float(cover["nu"]), "dpgf": 0.0}}, "comment": layout.TAG_OBJECT}
        surfaces.insert(0, {"radius": math.inf, "thickness": gap, "comment": layout.TAG_COVER_TOP})
    else:
        obj = {"thickness": ffd, "material": "", "comment": layout.TAG_OBJECT}

    m = tube_lens_mm / f_target
    printed_fn = header.get("field_number") or (2 * header["image_height"] if header.get("image_height") else None)
    if printed_fn and header.get("tube_lens_f"):
        # 专利的视场数对应它自己的筒镜；换到参考筒镜时保持物方视场不变：OFN × f_TL / f_tube
        printed_fn = float(printed_fn) * tube_lens_mm / float(header["tube_lens_f"])
    fn = field_number or printed_fn or 22.0
    radius = float(fn) / (2 * m)
    presc = {
        "title": f"{(ex.get('source') or {}).get('patent', '')} {ex.get('embodiment', '')} f={f_target:g} mm",
        "aperture": {"type": "object_na", "value": na},
        "fields": {"type": "object_height", "y": [0.0, 0.5 * radius, 0.7 * radius, radius]},
        "wavelengths": {"um": WAVES[wave], "weights": [1.0, 1.0, 1.0], "primary": 2},
        "catalogs": list(dict.fromkeys(g["catalog"] for g in glasses.values() if g["catalog"])) or catalogs[:1],
        "ray_aiming": "real",
        "telecentric_object": stop == "rear_focal",
        "object": obj,
        "surfaces": surfaces,
    }
    prescription.normalize(presc)

    expect: dict[str, Any] = {"efl": float(header["f"]) * scale, "na": na, "rel_tol": 0.01}
    if header.get("WD") is not None:
        expect["wd"] = float(header["WD"]) * scale
    # 原文数据的印刷精度决定了复现所能达到的精度：用舍入包络的半宽作为各项容差的下限
    envelope = rounding_envelope(rows, cover)
    expect["tolerance_mm"] = {k: (hi - lo) / 2 * scale for k, (lo, hi) in envelope.items()}
    report = {
        "scale": scale,
        "focal_length_mm": f_target,
        "field_number": float(fn),
        "paraxial": {"wavelength": wave, "efl_mm": efl, "front_focal_distance_mm": ffd, "back_focal_distance_mm": bfd},
        "stop": stop,
        "cover_glass": cover,
        "cover_glass_assumed": bool(cover_assumptions),
        "printed_decimals": column_decimals(rows),
        "rounding_envelope": {k: [lo * scale, hi * scale] for k, (lo, hi) in envelope.items()},
        "elements": sum(1 for r in rows if "n" in r),
        "glasses": [
            {"n": n, "nu": nu, "printed": name or None, "material": g["material"], "catalog": g["catalog"], "status": g["status"], "how": g["how"], "candidates": g["candidates"]}
            for (n, nu, name), g in glasses.items()
        ],
        "assumptions": [
            f"专利折射率按 {wave} 线处理" + ("（专利未注明，按 d 线假设）" if not ex.get("wavelength") else ""),
            "光阑放在物镜后焦面（物方远心）" if stop == "rear_focal" else f"光阑按专利（{stop}）",
            f"焦距 {f_target:g} mm（缩放 {scale:g}），参考筒镜 {tube_lens_mm:g} mm",
            f"物镜视场数 {fn:g}" + ("" if (field_number or header.get("field_number") or header.get("image_height")) else "（专利未给，按默认 22 假设）"),
        ]
        + cover_assumptions,
    }
    return {"prescription": presc, "expect": expect, "report": report}
