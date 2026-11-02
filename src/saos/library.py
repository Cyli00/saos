"""共享参考库（<repo>/library）的统一只读入口：先 catalog() 看有哪些库、各多少条、按什么查，再 find() 按文本或字段取条目，
条目带上它引用的来源。按规格的检索（专利、起点、显微物镜）仍用各自的函数，catalog() 写明是哪个。权威数据是各库的文件，这里不缓存、不改写。
One read-only entry to the shared reference libraries: catalog() lists them with their entry counts and how to query
them; find() returns entries by text or field, each with the sources it cites. Specification searches stay in their own
functions, which catalog() names. The library files are the authority; nothing here caches or writes them."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable

from saos import config
from saos.protocol import ErrorCode, SaosError, read_json, read_jsonl

FIND_LIMIT = 20
_SOURCE_KEYS = ("source", "source_ids", "sources")


def root() -> Path:
    return config.REPO_ROOT / "library"


def _file(*parts: str) -> Path:
    path = root().joinpath(*parts)
    if not path.is_file():
        raise SaosError(ErrorCode.FILE_NOT_FOUND, f"library {parts[0]} is missing {'/'.join(parts[1:])}", {"path": str(path)})
    return path


def _entries_dir(name: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    return [read_json(p) for p in sorted((root() / name / "entries").glob("*/entry.json"))], {}


def _materials() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = read_json(_file("materials", "catalog.json"))
    return data["materials"], data["sources"]


def _coatings() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest = read_json(_file("coatings", "manifest.json"))
    designs = read_json(_file("coatings", "designs.json"))
    rows = [{"kind": "layer_material", **m} for m in manifest["materials"]]
    rows += [{"kind": "design", **d} for d in designs["designs"]]
    rows += [{"kind": "design_abstract_only", **d} for d in designs.get("abstract_only", [])]
    return rows, designs["sources"]


def _thermal() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = read_json(_file("thermal", "dndt.json"))
    rows = [{"kind": "dndt_table", **t} for t in data["dndt_tables"]]
    rows += [{"kind": "cte_manufacturer", **c} for c in data["cte_manufacturer"]]
    return rows, data["sources"]


def _mechanics() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = read_json(_file("mechanics", "materials.json"))
    return data["materials"], data["sources"]


def _glasscat() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    return read_json(_file("glasscat", "manifest.json"))["catalogs"], {}


def _patents() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    return read_jsonl(_file("patents", "index.jsonl")), {}


# 每个库：读法、用途、按规格检索的函数（没有则 None）/ per library: loader, purpose, the specification search if any
LIBRARIES: dict[str, tuple[Callable[[], tuple[list[dict[str, Any]], dict[str, Any]]], str, str | None]] = {
    "materials": (_materials, "optical adhesives and printable resins: reported indices, dispersion, sources", "saos.library.index(id, wavelength_nm)"),
    "coatings": (_coatings, "coating layer materials (n, k tables for SAOS_COATING.DAT) and checked published designs", None),
    "thermal": (_thermal, "manufacturer dn/dT tables and expansion coefficients that differ from the local catalogs", None),
    "mechanics": (_mechanics, "mirror blank, barrel and spacer materials: expansion, density, modulus, with a source grade", None),
    "glasscat": (_glasscat, "in-production glass catalogs installed into OpticStudio (discontinued grades removed)", None),
    "patents": (_patents, "2456 patent examples indexed by computed paraxial features (original .seq files stay local)",
                "saos.starts.patents.search(half_field_deg=, f_number=, wavelengths_um=, ...)"),
    "starts": (lambda: _entries_dir("starts"), "classic starting forms with sources and applicability",
               "saos.starts.generate / saos.starts.generate.query"),
    "microscope": (lambda: _entries_dir("microscope"), "microscope objective patent examples, benchmarks and calibration evidence",
                   "saos microscope library query (saos microscope library --help)"),
}


def _load(name: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if name not in LIBRARIES:
        raise SaosError(ErrorCode.BAD_ARGS, f"no library {name!r}; one of {sorted(LIBRARIES)}")
    return LIBRARIES[name][0]()


def catalog() -> list[dict[str, Any]]:
    """每个库：名字、用途、当前条目数、条目的字段、README 与按规格检索的函数。条目数现读文件，库缺文件时 entries 为 None 并写明原因。
    Each library with its purpose, current entry count, entry fields, README and specification search."""
    out = []
    for name, (_, purpose, search) in LIBRARIES.items():
        item: dict[str, Any] = {"library": name, "purpose": purpose, "readme": f"library/{name}/README.md", "search": search}
        try:
            rows, _ = _load(name)
            item["entries"] = len(rows)
            item["fields"] = sorted({k for r in rows for k in r})
        except SaosError as exc:
            item["entries"], item["fields"], item["error"] = None, [], exc.message
        out.append(item)
    return out


def _cited(row: dict[str, Any], sources: dict[str, Any]) -> dict[str, Any]:
    ids: list[str] = []
    for key in _SOURCE_KEYS:
        value = row.get(key)
        ids += [value] if isinstance(value, str) else [v for v in value if isinstance(v, str)] if isinstance(value, list) else []
    return {i: sources[i] for i in ids if i in sources}


def find(library: str, text: str | None = None, *, limit: int = FIND_LIMIT, **fields: Any) -> dict[str, Any]:
    """一个库里的条目：text 在条目任一处出现（不分大小写），且 fields 里每个顶层字段都相等。返回 total 与前 limit 条，
    每条的 cited 是它引用的来源全文。不认识的字段报 BAD_ARGS 并列出可用字段。
    Entries of one library containing text anywhere (case-insensitive) whose top-level fields equal the given ones;
    total plus the first limit entries, each with the full sources it cites. An unknown field is BAD_ARGS."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise SaosError(ErrorCode.BAD_ARGS, f"limit must be a positive integer, got {limit!r}")
    rows, sources = _load(library)
    known = {k for r in rows for k in r}
    unknown = sorted(set(fields) - known)
    if unknown:
        raise SaosError(ErrorCode.BAD_ARGS, f"{library} entries have no field {unknown}", {"fields": sorted(known)})
    needle = text.lower() if text else None
    hits = [r for r in rows
            if all(r.get(k) == v for k, v in fields.items())
            and (needle is None or needle in json.dumps(r, ensure_ascii=False).lower())]
    return {"library": library, "total": len(hits),
            "items": [{**r, "cited": _cited(r, sources)} for r in hits[:limit]]}


def refractive_index(material: dict[str, Any], wavelength_nm: float) -> float:
    """材料库一条记录在 wavelength_nm 的折射率；没有色散模型或超出它的有效范围时报 BAD_ARGS（不外推）。
    The index of a materials-library record; BAD_ARGS without a dispersion model or outside its valid range (no extrapolation)."""
    model = material.get("dispersion")
    if model is None:
        raise SaosError(ErrorCode.BAD_ARGS, f"{material['id']} has no dispersion model; only its reported indices are known")
    lo, hi = model["valid_range_nm"]
    w = float(wavelength_nm)
    if not (math.isfinite(w) and lo <= w <= hi):
        raise SaosError(ErrorCode.BAD_ARGS, f"{material['id']}: wavelength must be within {lo}–{hi} nm (no extrapolation), got {wavelength_nm!r}")
    c = model["coefficients"]
    if model["formula"] == "cauchy_nm":
        return c[0] + c[1] / w**2 + c[2] / w**4
    if model["formula"] == "sellmeier1_um":
        w2 = (w / 1000) ** 2
        return math.sqrt(1 + sum(c[i] * w2 / (w2 - c[i + 1]) for i in (0, 2, 4)))
    raise SaosError(ErrorCode.BAD_ARGS, f"{material['id']}: unsupported dispersion formula {model['formula']!r}")


def index(material_id: str, wavelength_nm: float) -> float:
    """材料库 ID 的折射率（见 refractive_index）。/ The index of a materials-library ID at wavelength_nm."""
    rows, _ = _load("materials")
    for m in rows:
        if m["id"] == material_id:
            return refractive_index(m, wavelength_nm)
    raise SaosError(ErrorCode.BAD_ARGS, f"no material {material_id!r} in library/materials", {"ids": [m["id"] for m in rows]})
