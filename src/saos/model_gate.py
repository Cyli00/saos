"""模型确认关卡：用户认可建模说明之后才能记录调试轮次；含模型玻璃的镜头不能交付。
Model approval gate: rounds can be recorded only after the user approved a model description;
a lens that still contains a model glass cannot be delivered.

    saos lens describe --job J --notes N.json      # models/NNN-<lens>/：页面、model.json（含结构指纹）
    saos lens approve  --job J --model models/NNN-<lens> --quote "<用户原话 / the user's words>"

结构指纹只取“模型是什么”：系统设置（孔径、远心、光线瞄准、视场、波长）和逐面的面型、介质种类、孔径、光阑，
以及决定几何布局的量：坐标断点的全部参数和厚度、面属性里的倾斜与偏心、菲涅尔类面的基底半径和圆锥（基底就是实物表面）。
半径、厚度、圆锥、多项式系数和具体材料牌号是设计变量，优化中可以变，不影响指纹；
材料牌号逐轮记录在 ledger 里，在交付报告中列出。
The structural fingerprint keeps only what the model *is*: system settings (aperture, telecentricity,
ray aiming, fields, wavelengths) and, per surface, the surface type, the kind of medium, the hard
aperture and the stop, plus what fixes the layout: every parameter and the thickness of a coordinate
break, the tilt/decenter of a surface, and the substrate radius and conic of Fresnel-type surfaces
(their substrate is the physical surface). Radii, thicknesses, conics, polynomial terms and the chosen material names are
design variables and may change during optimisation without a new approval; material names are
recorded in every round and listed in the delivery report.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from saos import jobs
from saos.protocol import ErrorCode, SaosError, write_json

APPROVAL_FILE = "approval.json"


def _num(v: Any) -> Any:
    # 指纹里的数值统一舍入，避免浮点读回的最后几位造成误判
    # round numbers so that the last bits of a float read back from OpticStudio do not count as a change
    if isinstance(v, float):
        return "inf" if math.isinf(v) else round(v, 9)
    return v


def medium_kind(surface: dict[str, Any]) -> str:
    return str((surface.get("material_source") or {}).get("kind", "none"))


def fingerprint(desc: dict[str, Any]) -> dict[str, Any]:
    ap = desc["aperture"]
    surfaces = []
    for s in desc["surfaces"]:
        aperture = {k: _num(v) for k, v in (s.get("aperture") or {}).items()}
        item = {"type": s["type"], "medium": medium_kind(s), "aperture": aperture, "stop": bool(s.get("is_stop"))}
        if s["type"] == "Coordinate Break":
            item["break"] = {**{p["name"]: _num(p["value"]) for p in s.get("params") or []}, "thickness": _num(float(s["thickness"]))}
        if s.get("tilt_decenter"):
            item["tilt_decenter"] = {k: _num(v) for k, v in s["tilt_decenter"].items()}
        if "Fresnel" in str(s["type"]):
            item["substrate"] = [_num(float(s["radius"])), _num(float(s.get("conic", 0.0)))]
        surfaces.append(item)
    return {
        "aperture": {k: _num(ap.get(k)) for k in ("type", "value", "telecentric_object", "afocal_image")},
        "ray_aiming": desc.get("ray_aiming"),
        "fields": {"type": desc["fields"]["type"], "normalization": desc["fields"].get("normalization"),
                   "items": [[_num(f["x"]), _num(f["y"])] for f in desc["fields"]["items"]]},
        "wavelengths": [[_num(w["um"]), bool(w["primary"])] for w in desc["wavelengths"]],
        "surfaces": surfaces,
    }


def digest(fp: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(fp, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def materials(desc: dict[str, Any]) -> list[dict[str, Any]]:
    """逐面的介质及来源，记入每个调试轮次。 / Medium and source per surface, stored with every round."""
    out = []
    for s in desc["surfaces"]:
        src = s.get("material_source") or {}
        if src.get("kind") in ("model", "catalog"):
            item = {"surface": s["index"], "kind": src["kind"], "n": s.get("n")}
            item.update({"name": src.get("name"), "catalogs": src.get("catalogs")} if src["kind"] == "catalog"
                        else {"nd": src.get("nd"), "vd": src.get("vd"), "dpgf": src.get("dpgf")})
            out.append(item)
    return out


def model_glass_surfaces(desc: dict[str, Any]) -> list[int]:
    return [s["index"] for s in desc["surfaces"] if medium_kind(s) == "model"]


def diff(a: Any, b: Any, path: str = "") -> list[str]:
    """两份数据中不同的路径。 / Paths where two data trees differ."""
    if isinstance(a, dict) and isinstance(b, dict):
        return [d for k in sorted(set(a) | set(b)) for d in diff(a.get(k), b.get(k), f"{path}.{k}" if path else k)]
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [d for k, (x, y) in enumerate(zip(a, b)) for d in diff(x, y, f"{path}[{k}]")]
    return [] if a == b else [path or "(root)"]


def approve(job: jobs.Job, model: str, quote: str) -> dict[str, Any]:
    folder = (job.root / model).resolve()
    if job.root.resolve() not in folder.parents or folder.parent.name != "models":
        raise SaosError(ErrorCode.BAD_ARGS, "model must be a folder under the job's models/, e.g. models/001-start")
    data_file = folder / "model.json"
    if not data_file.is_file():
        raise SaosError(ErrorCode.FILE_NOT_FOUND, f"{model} has no model.json; run lens describe first")
    if not quote.strip():
        raise SaosError(ErrorCode.BAD_ARGS, "quote must keep the user's own words of approval")
    notes_file = job.root / "notes" / f"model-{folder.name.split('-', 1)[0]}.json"
    if not notes_file.is_file():
        raise SaosError(ErrorCode.FILE_NOT_FOUND, f"{model} was described without notes; run lens describe --notes with what each surface is, "
                        "the assumptions and the first-order analysis, then show the new page to the user")
    data = json.loads(data_file.read_text(encoding="utf-8"))
    record = {"model": folder.relative_to(job.root).as_posix(), "lens": data.get("lens"), "quote": quote.strip(),
              "time": jobs.now_iso(), "fingerprint_sha": data["fingerprint_sha"],
              # 用户确认时页面上仍有的自动检查结果，一并留档 / warnings still on the page when the user approved
              "warnings_acknowledged": data.get("warnings", [])}
    write_json(folder / APPROVAL_FILE, record)
    job.seal(folder / APPROVAL_FILE)
    return record


def approvals(job: jobs.Job) -> list[dict[str, Any]]:
    root = job.root / "models"
    out = []
    for f in sorted(root.glob(f"*/{APPROVAL_FILE}")) if root.is_dir() else []:
        record = json.loads(f.read_text(encoding="utf-8"))
        model = json.loads((f.parent / "model.json").read_text(encoding="utf-8"))
        env = ((model.get("notes") or {}).get("environment")) or []
        out.append({**record, "fingerprint": model["fingerprint"], "environment": [int(i) for i in env]})
    return out


def require_approved(job: jobs.Job, desc: dict[str, Any]) -> dict[str, Any]:
    """当前镜头的结构必须与某个已确认的建模说明一致；否则报错并指出变了什么。
    The current lens must match an approved model description; otherwise fail and say what changed."""
    fp = fingerprint(desc)
    sha = digest(fp)
    found = approvals(job)
    for record in reversed(found):
        if record["fingerprint_sha"] == sha:
            return {"model": record["model"], "fingerprint_sha": sha, "environment": record["environment"]}
    details: dict[str, Any] = {"fingerprint_sha": sha}
    if found:
        details["latest_approved"] = found[-1]["model"]
        details["changed"] = diff(found[-1]["fingerprint"], fp)[:30]
        message = ("the model changed since the last approved description (see details.changed); "
                   "run lens describe, show the page to the user and lens approve with their words before recording rounds")
    else:
        message = "no approved model description; run lens describe, show the page to the user and lens approve with their words before recording rounds"
    raise SaosError(ErrorCode.MODEL_NOT_APPROVED, message, details)
