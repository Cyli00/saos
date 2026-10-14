"""任务的材料来源记录：sources/materials.yaml。镜头里每种真实材料都要在这里写明来源。
The job's material source record, sources/materials.yaml: every real material in the lens needs its source here.

字段沿用 library/materials/catalog.json（项目材料库）/ fields follow library/materials/catalog.json:

    schema: 1
    sources:
      darus-6471-v1: {url: https://doi.org/10.18419/DARUS-6471, title: ..., retrieved: 2026-10-14, version: "1.0", license: CC BY 4.0}
    materials:
      - id: lens-resin                       # 任务内的名字 / name inside the job
        zemax: {catalog: SAOS_MICRO_20261014, name: SAOS_IPS_20261014}   # 在 OpticStudio 中的目录和牌号 / catalog and name in OpticStudio
        library_id: ips-darus2026            # 来自 library/materials 时填它，来源已在库里 / when taken from the library
      - id: prism
        zemax: {catalog: SCHOTT, name: N-BK7}    # OpticStudio 自带的厂家目录本身就是来源 / a vendor catalog is a source itself
      - id: glue
        zemax: {catalog: JOB_GLUE, name: GLUE1}
        name: ..., manufacturer: ..., state: cured, source_ids: [vendor-tds]
        dispersion: {formula: cauchy_nm, coefficients: [...], valid_range_nm: [435.8, 1500], temperature_C: 25, source_id: vendor-tds}

来源三选一即可：library_id、source_ids（指向上面的 sources）、或 OpticStudio 自带的厂家目录。
A material is sourced by one of: library_id, source_ids (keys of sources), or a vendor catalog shipped with OpticStudio.
"""

from __future__ import annotations

from typing import Any

import yaml

from saos import jobs
from saos.protocol import ErrorCode, SaosError

FILE = "sources/materials.yaml"


def _bad(message: str) -> SaosError:
    return SaosError(ErrorCode.BAD_ARGS, f"{FILE}: {message}")


def validate(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise _bad("must be a mapping with schema: 1")
    sources = data.get("sources") or {}
    if not isinstance(sources, dict) or any(not isinstance(v, dict) or not v.get("url") for v in sources.values()):
        raise _bad("sources must map an id to {url, title, retrieved, version, ...}")
    materials = data.get("materials")
    if not isinstance(materials, list) or not materials:
        raise _bad("materials must be a non-empty list")
    for m in materials:
        zemax = (m or {}).get("zemax") if isinstance(m, dict) else None
        if not isinstance(zemax, dict) or not zemax.get("name") or not m.get("id"):
            raise _bad(f"each material needs id and zemax.name: {m!r}")
        missing = [s for s in m.get("source_ids") or [] if s not in sources]
        if missing:
            raise _bad(f"{m['id']}: source_ids {missing} are not listed under sources")
        if not (m.get("library_id") or m.get("source_ids") or zemax.get("catalog")):
            raise _bad(f"{m['id']}: give library_id, source_ids or the OpticStudio catalog it comes from")
    return data


def recorded_names(job: jobs.Job) -> set[str] | None:
    """记录中的 OpticStudio 牌号（大写）；没有记录文件时为 None。 / Recorded names (upper case); None without a record."""
    path = job.root / FILE
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise _bad(f"cannot parse: {exc}") from exc
    return {str(m["zemax"]["name"]).upper() for m in validate(data)["materials"]}
