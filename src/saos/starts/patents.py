"""专利镜头库（library/patents）：从 CODE V .seq 原文件建索引、按规格检索、转成处方或作为 starts.generate 的候选。

索引里的数值是用 nd 算的近轴量（seq.paraxial），只作检索与预筛；标签由这些数值按 README 写明的阈值导出，不是原专利的分类。
"""
from __future__ import annotations

import datetime
import json
import math
import re
from pathlib import Path
from typing import Any

from saos import config, jobs
from saos.protocol import ErrorCode, SaosError, read_jsonl
from saos.starts import seq

SCHEMA = 1
COUNTRIES = {"JAPAN": "JP", "USA": "US", "US": "US", "U.S.A": "US", "USSR": "SU"}
ID_RE = re.compile(r"^(or\d{5})(?:-p(\d+))?$")
# 标签阈值（README 同步）：远摄比 TTL/EFL < 1，反远摄 BFL/EFL > 1
TELEPHOTO_RATIO = 1.0
RETROFOCUS_RATIO = 1.0


def _bad(message: str, **details) -> SaosError:
    return SaosError(ErrorCode.BAD_ARGS, message, details or None)


def library_root() -> Path:
    return config.REPO_ROOT / "library" / "patents"


def _source(title: str) -> dict[str, Any]:
    words = title.split()
    upper = [w.upper() for w in words]
    country = COUNTRIES.get(upper[0]) if words else None
    number = words[upper.index("PATENT") + 1] if "PATENT" in upper and upper.index("PATENT") + 1 < len(words) else None
    rest = " ".join(words[upper.index("PATENT") + 2:]) if number else ""
    return {"country": country, "number": number, "rest": rest}


def _ratio(a: float | None, b: float | None) -> float | None:
    return a / b if a is not None and b else None


def _position_record(data: dict[str, Any], position: int | None) -> dict[str, Any]:
    px = seq.paraxial(data, position)
    efl = abs(px["efl"])
    record = {k: px[k] for k in ("efl", "f_number", "half_field_deg", "object_height", "image_height", "magnification") if k in px}
    if "total_track" in px and data["object"]["thickness"] >= seq.INFINITE_OBJECT:
        record["ttl_over_efl"] = px["total_track"] / efl
        record["bfl_over_efl"] = px["bfl"] / efl
    if data["surfaces"][-1]["pim"] and data["afocal_image"] is None:
        record["pim_residual"] = abs(px["image_distance"] - px["last_thickness"]) / efl
    return record


def describe(data: dict[str, Any], path: Path, root: Path) -> dict[str, Any]:
    """一个 .seq 文件的索引记录。"""
    media = [seq.material(s["token"], data["private"]) for s in data["surfaces"]]
    kinds: dict[str, list[str]] = {}
    for m in media:
        if m["kind"] not in ("air", "code", "mirror"):
            kinds.setdefault(m["kind"], [])
            if m["raw"] not in kinds[m["kind"]]:
                kinds[m["kind"]].append(m["raw"])
    blockers = seq.problems(data) + [f"不认识的命令 {u}" for u in data["unrecognized"]]
    needs = sorted({m["raw"] for m in media if m["kind"] in ("special", "private", "unknown")})
    record: dict[str, Any] = {
        "schema": SCHEMA, "id": path.stem, "file": path.relative_to(root).as_posix(), "sha256": jobs.sha256_file(path),
        "title": data["title"], "source": _source(data["title"]), "units": data["units"],
        "wavelengths_nm": data["wavelengths_nm"], "reference": data["reference"],
        "conjugate": "afocal" if data["afocal_image"] is not None else "infinite" if data["object"] and data["object"]["thickness"] >= seq.INFINITE_OBJECT else "finite",
        "aperture": data["aperture"], "field_type": data["fields"]["type"] if data["fields"] else None,
        "surfaces": len(data["surfaces"]), "aspheres": sum(s["asphere"] is not None or bool(s["conic"]) for s in data["surfaces"]),
        "zoom": data["zoom"]["positions"] if data["zoom"] else None,
        "materials": {"codes": sum(m["kind"] == "code" for m in media), **kinds},
        "materials_to_map": needs, "private_glasses": data["private"] or None,
        "blockers": blockers, "convertible": not blockers,
    }
    positions = list(range(1, data["zoom"]["positions"] + 1)) if data["zoom"] else [None]
    try:
        record.update(seq.elements(data, positions[0]))
        record["paraxial"] = [_position_record(data, p) for p in positions]
        record["paraxial_error"] = None
    except SaosError as exc:
        record.setdefault("elements", None)
        record["paraxial"], record["paraxial_error"] = None, exc.message
    if data["zoom"] and record["paraxial"]:
        efls = [p["efl"] for p in record["paraxial"]]
        record["zoom_ratio"] = max(map(abs, efls)) / min(map(abs, efls))
    record["tags"] = _tags(record)
    return record


def _tags(r: dict[str, Any]) -> list[str]:
    tags = [r["conjugate"]]
    if r["zoom"]:
        tags.append("zoom")
    if r["aspheres"]:
        tags.append("aspheric")
    if r.get("mirrors"):
        tags.append("catadioptric" if r.get("elements") else "reflective")
    if r.get("signs") and "(" in r["signs"]:
        tags.append("cemented")
    if not r["materials_to_map"] and "catalog" not in r["materials"]:
        tags.append("model_glass_only")
    first = (r.get("paraxial") or [{}])[0]
    if first.get("ttl_over_efl") is not None and first["ttl_over_efl"] < TELEPHOTO_RATIO:
        tags.append("telephoto")
    if first.get("bfl_over_efl") is not None and first["bfl_over_efl"] > RETROFOCUS_RATIO:
        tags.append("retrofocus")
    return tags


def build_index(root: Path | None = None) -> dict[str, Any]:
    """重建 index.jsonl（按 ID 排序）；返回条数统计。解析出错的文件也写一条，blockers 写明原因。"""
    root = root or library_root()
    files = sorted((root / "seq").glob("*.seq"))
    if not files:
        raise SaosError(ErrorCode.FILE_NOT_FOUND, f"{root / 'seq'} 下没有 .seq 文件")
    records = []
    for path in files:
        try:
            records.append(describe(seq.read(path), path, root))
        except SaosError as exc:
            records.append({"schema": SCHEMA, "id": path.stem, "file": path.relative_to(root).as_posix(), "sha256": jobs.sha256_file(path),
                            "blockers": [f"解析失败：{exc.message}"], "convertible": False, "tags": []})
    text = "".join(json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n" for r in records)
    target = root / "index.jsonl"
    tmp = target.with_suffix(".jsonl.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)
    return {"entries": len(records), "convertible": sum(r["convertible"] for r in records),
            "paraxial": sum(bool(r.get("paraxial")) for r in records), "built": datetime.date.today().isoformat()}


def load_index(root: Path | None = None) -> list[dict[str, Any]]:
    root = root or library_root()
    path = root / "index.jsonl"
    if not path.is_file():
        raise SaosError(ErrorCode.FILE_NOT_FOUND, f"没有专利库索引 {path}；用 saos.starts.patents.build_index() 生成")
    return read_jsonl(path)


def _covered(value: float | None, limit: float | None, faster_is_harder: bool) -> float | None:
    """需求相对条目的压力：≤ 1 表示条目原例覆盖该需求。"""
    if value is None or limit is None or limit <= 0 or value <= 0:
        return None
    return limit / value if faster_is_harder else value / limit


def search(*, half_field_deg: float | None = None, f_number: float | None = None, wavelengths_um: list[float] | None = None,
           magnification: float | None = None, conjugate: str = "infinite", elements: tuple[int | None, int | None] = (None, None),
           zoom: bool = False, aspheric: bool | None = None, reflective: bool = False, convertible_only: bool = True,
           ttl_over_efl_max: float | None = None, bfl_over_efl_min: float | None = None, signs: str | None = None,
           country: str | None = None, allow_extrapolation: bool = False, limit: int = 20,
           root: Path | None = None) -> dict[str, Any]:
    """按规格检索。半视场、f/#、波段与原例比较：原例覆盖需求的排前面，越接近需求越靠前，再按片数少的优先。
    变焦镜头（zoom=True）逐个位置比较，结果给出最接近的位置。返回 {"candidates", "excluded", "total"}，excluded 是各原因的条数。"""
    if conjugate not in ("infinite", "finite", "afocal"):
        raise _bad("conjugate 取 infinite、finite 或 afocal")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise _bad("limit 必须为正整数")
    pattern = re.compile(signs) if signs else None
    lo_need = min(wavelengths_um) * 1000 if wavelengths_um else None
    hi_need = max(wavelengths_um) * 1000 if wavelengths_um else None
    excluded: dict[str, int] = {}
    found = []

    def drop(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    records = load_index(root)
    for r in records:
        if convertible_only and not r["convertible"]:
            drop("不能直接转换（blockers）")
            continue
        if not r.get("paraxial"):
            drop("没有近轴数据（材料无 nd 或无焦）")
            continue
        if r["conjugate"] != conjugate:
            drop("共轭不同")
            continue
        if bool(r["zoom"]) != zoom:
            drop("变焦与否不符")
            continue
        if aspheric is not None and bool(r["aspheres"]) != aspheric:
            drop("非球面条件不符")
            continue
        if not reflective and r.get("mirrors"):
            drop("含反射面")
            continue
        n = r.get("elements")
        if (elements[0] is not None and (n is None or n < elements[0])) or (elements[1] is not None and (n is None or n > elements[1])):
            drop("片数不符")
            continue
        if pattern and not (r.get("signs") and pattern.search(r["signs"])):
            drop("光焦度符号不符")
            continue
        if country and r["source"]["country"] != country:
            drop("国别不符")
            continue
        band_lo, band_hi = min(r["wavelengths_nm"]), max(r["wavelengths_nm"])
        best = None
        for position, p in enumerate(r["paraxial"], start=1):
            if ttl_over_efl_max is not None and (p.get("ttl_over_efl") is None or p["ttl_over_efl"] > ttl_over_efl_max):
                continue
            if bfl_over_efl_min is not None and (p.get("bfl_over_efl") is None or p["bfl_over_efl"] < bfl_over_efl_min):
                continue
            pressure = {"half_field_deg": _covered(half_field_deg, p.get("half_field_deg"), False),
                        "f_number": _covered(f_number, p.get("f_number"), True),
                        "magnification": _covered(abs(magnification), abs(p["magnification"]), False) if magnification and p.get("magnification") else None}
            unknown = [k for k, need in (("half_field_deg", half_field_deg), ("f_number", f_number), ("magnification", magnification))
                       if need is not None and pressure[k] is None]
            pressure = {k: v for k, v in pressure.items() if v is not None}
            if lo_need is not None:
                pressure["wavelengths"] = 1.0 if band_lo - 1 <= lo_need and hi_need <= band_hi + 1 else math.inf
            exceeded = [k for k, v in pressure.items() if v > 1 + 1e-9]
            if unknown or (exceeded and not allow_extrapolation):
                continue
            closeness = max((abs(math.log(v)) if math.isfinite(v) else math.inf for v in pressure.values()), default=0.0)
            key = (bool(exceeded), closeness, n or 0)
            if best is None or key < best[0]:
                best = (key, position, pressure, exceeded, p)
        if best is None:
            drop("规格或比例不符")
            continue
        key, position, pressure, exceeded, p = best
        found.append((key, r["id"], {
            "id": r["id"] if not r["zoom"] else f"{r['id']}-p{position}", "file": r["file"], "title": r["title"], "source": r["source"],
            "position": position if r["zoom"] else None, "elements": n, "groups": r.get("groups"), "signs": r.get("signs"),
            "tags": r["tags"], "wavelengths_nm": r["wavelengths_nm"], "paraxial": p, "pressure": pressure, "extrapolation": exceeded,
            "materials_to_map": r["materials_to_map"], "zoom_ratio": r.get("zoom_ratio")}))
    found.sort(key=lambda item: (item[0], item[1]))
    return {"candidates": [c for _, _, c in found[:limit]], "matched": len(found), "excluded": excluded, "total": len(records)}


def _split_id(entry_id: str) -> tuple[str, int | None]:
    m = ID_RE.match(entry_id) if isinstance(entry_id, str) else None
    if not m:
        raise _bad(f"专利条目 ID 形如 or01234，变焦位置写成 or01234-p2：{entry_id!r}")
    return m[1], int(m[2]) if m[2] else None


def _record(index: list[dict[str, Any]], base: str) -> dict[str, Any]:
    for r in index:
        if r["id"] == base:
            return r
    raise SaosError(ErrorCode.FILE_NOT_FOUND, f"专利库索引里没有 {base}")


def prescription(entry_id: str, *, materials: dict[str, Any] | None = None, allow_unrecognized: bool = False,
                 root: Path | None = None) -> dict[str, Any]:
    """把一个条目转成 lens build 处方。原文件哈希与索引不符时拒绝（先重建索引）。
    返回 {"prescription", "provenance"}；provenance 写明原文件、哈希、位置、材料映射和未转换的命令。"""
    root = root or library_root()
    base, position = _split_id(entry_id)
    record = _record(load_index(root), base)
    path = root / record["file"]
    if not path.is_file():
        raise SaosError(ErrorCode.FILE_NOT_FOUND, f"缺原文件 {path}")
    sha = jobs.sha256_file(path)
    if sha != record["sha256"]:
        raise _bad(f"{path.name} 与索引不符（哈希变了）；先运行 build_index() 重建索引")
    converted = seq.to_prescription(seq.read(path), position=position, materials=materials, allow_unrecognized=allow_unrecognized)
    provenance = {"library": "library/patents", "id": entry_id, "file": record["file"], "sha256": sha, "title": record["title"],
                  "source": record["source"], "position": position, "materials": converted["materials"],
                  "not_converted": converted["not_converted"], "unrecognized": converted["unrecognized"]}
    return {"prescription": converted["prescription"], "provenance": provenance}


def start_entries(items: list[Any], root: Path | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """starts.generate 用的候选条目：items 是 ID，或 {"id", "materials", "allow_unrecognized"}。
    适用范围取该条目（该位置）的原例：半视场上限、f/# 下限、波段与共轭。缺这些量的条目放进排除表。"""
    root = root or library_root()
    if not isinstance(items, list) or not items:
        raise _bad("patents 必须是非空的条目列表")
    index = load_index(root)
    entries, excluded = [], []
    for item in items:
        spec = {"id": item} if isinstance(item, str) else item
        if not isinstance(spec, dict) or set(spec) - {"id", "materials", "allow_unrecognized"}:
            raise _bad(f"专利候选写成 ID 或 {{id, materials, allow_unrecognized}}：{item!r}")
        base, position = _split_id(spec.get("id"))
        r = _record(index, base)
        source = {"library": "library/patents", "patent": r["title"], "file": r["file"], "sha256": r["sha256"], **r["source"]}
        reasons = []
        if r["zoom"] and position is None:
            reasons.append(f"变焦镜头要指定位置（{base}-p1…{base}-p{r['zoom']}）")
        if position is not None and not 1 <= position <= (r["zoom"] or 0):
            reasons.append(f"{base} 没有位置 {position}" if r["zoom"] else f"{base} 不是变焦镜头，ID 不带 -p")
        paraxial = r.get("paraxial") or []
        p = paraxial[(position or 1) - 1] if not reasons and paraxial else None
        if p is None or p.get("half_field_deg") is None or p.get("f_number") is None:
            reasons.append("没有角视场或 f/# 的近轴数据（物高视场、无焦或材料无 nd）")
        if r["conjugate"] == "afocal":
            reasons.append("无焦像空间（AFI），generate 按像方 f/# 设孔径，不适用")
        if reasons:
            excluded.append({"id": spec["id"], "reasons": reasons, "source": source})
            continue
        entries.append({
            "id": spec["id"], "form": f"专利 {r['title']}；{r.get('elements')} 片 {r.get('signs') or ''}".strip(),
            "version": r["sha256"][:12], "status": "patent_unverified", "source": source,
            "metrics": {"elements": r.get("elements") or r["surfaces"]},
            "applicability": {"half_field_deg_max": p["half_field_deg"], "f_number_min": p["f_number"],
                              "wavelength_um": [min(r["wavelengths_nm"]) / 1000, max(r["wavelengths_nm"]) / 1000],
                              "conjugate": r["conjugate"]},
            "notes": ["范围取自专利原例的近轴量（nd 计算），不代表该形式的能力边界；未经本仓库复现验收。"],
            "patent": {"materials": spec.get("materials"), "allow_unrecognized": bool(spec.get("allow_unrecognized", False))}})
    return entries, excluded
