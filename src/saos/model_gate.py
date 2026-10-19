"""模型确认关卡：用户认可建模说明之后才能记录调试轮次；含模型玻璃的镜头不能交付。
Model approval gate: rounds can be recorded only after the user approved a model description;
a lens that still contains a model glass cannot be delivered.

    saos lens describe --job J --notes N.json      # models/NNN-<lens>/：页面、model.json（含结构指纹）
    saos lens approve  --job J --model models/NNN-<lens> --quote "<用户原话 / the user's words>"

结构指纹只取“模型是什么”，按零件来源分两种：
- 本设计要做的面：面型、介质类别（空气、玻璃、反射镜）、孔径类型、光阑、菲涅尔类面的基底是否为平面、是否带倾斜或偏心。
  半径、厚度、圆锥、多项式系数、倾斜角、坐标断点参数、孔径大小和具体材料都是设计变量，优化中可以变，不需要重新确认。
- 建模说明 notes.environment 中声明的样品、介质和外购件：再加上材料牌号、半径、圆锥、孔径参数、倾斜与偏心，不能为过关改动物理环境。
系统设置（孔径、远心、光线瞄准、视场、波长）也在指纹中；视场和波长允许在末尾追加采样点，已确认的点不能改，
因为检查按视场和波长的编号取值。材料牌号逐轮记录在 ledger 里，在交付报告中列出。
The structural fingerprint keeps only what the model *is*, in two kinds:
- surfaces this design makes: surface type, medium class (air, glass, mirror), aperture type, the stop, whether a
  Fresnel-type substrate is flat, whether the surface is tilted or decentred. Radii, thicknesses, conics, polynomial
  terms, tilt angles, coordinate-break parameters, aperture sizes and the chosen materials are design variables.
- surfaces declared as environment in notes.environment (sample, media, bought-in parts): also the material, radius,
  conic, aperture parameters and tilt/decenter, so the physical setting cannot change to pass a gate.
System settings (aperture, telecentricity, ray aiming, fields, wavelengths) are included; fields and wavelengths may gain
sampling points at the end, but approved points cannot change, because checks address them by number.
"""

from __future__ import annotations

import hashlib
import json
import math
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


def medium_class(surface: dict[str, Any]) -> str:
    """本设计要做的面只看介质类别：模型玻璃换成目录材料属于换材料，不是改结构。
    For a surface this design makes only the class of medium counts: a model glass replaced by a catalog glass is a material change."""
    kind = medium_kind(surface)
    return "glass" if kind in ("model", "catalog") else kind


def _tilted(s: dict[str, Any]) -> bool:
    if s.get("tilt_decenter"):
        return True
    return s["type"] == "Coordinate Break" and any(_num(p["value"]) not in (0, 0.0) for p in s.get("params") or [])


def fingerprint(desc: dict[str, Any], environment: Any = ()) -> dict[str, Any]:
    ap = desc["aperture"]
    given = {int(i) for i in environment or ()}
    surfaces = []
    for s in desc["surfaces"]:
        aperture = s.get("aperture") or {}
        item: dict[str, Any] = {"type": s["type"], "stop": bool(s.get("is_stop"))}
        if s["index"] in given:
            item.update(medium=medium_kind(s), material=str(s.get("material", "")).upper(),
                        aperture={k: _num(v) for k, v in aperture.items()},
                        radius=_num(float(s["radius"])), conic=_num(float(s.get("conic", 0.0))))
            if s["type"] == "Coordinate Break":
                item["break"] = {p["name"]: _num(p["value"]) for p in s.get("params") or []}
            if s.get("tilt_decenter"):
                item["tilt_decenter"] = {k: _num(v) for k, v in s["tilt_decenter"].items()}
        else:
            item.update(medium=medium_class(s), aperture={"type": aperture.get("type")}, tilted=_tilted(s))
            if "Fresnel" in str(s["type"]):
                # 平基底和弯基底是两种零件（实测光纤任务中基底被优化弯后穿透了板厚）
                # a flat and a curved substrate are different parts (in the fiber job an optimised substrate bent through the plate)
                item["substrate_flat"] = math.isinf(float(s["radius"]))
        surfaces.append(item)
    return {
        "aperture": {k: _num(ap.get(k)) for k in ("type", "value", "telecentric_object", "afocal_image")},
        "ray_aiming": desc.get("ray_aiming"),
        "fields": {"type": desc["fields"]["type"], "normalization": desc["fields"].get("normalization"),
                   "items": [[_num(f["x"]), _num(f["y"])] for f in desc["fields"]["items"]]},
        "wavelengths": [[_num(w["um"]), bool(w["primary"])] for w in desc["wavelengths"]],
        "surfaces": surfaces,
    }


def structure_changes(approved: dict[str, Any], current: dict[str, Any], environment: Any = ()) -> list[str]:
    """已确认模型与当前镜头在结构上的不同之处；视场和波长在末尾追加的采样点不算。
    Structural differences between an approved model and the current lens; sampling points appended to fields or wavelengths do not count."""
    a, b = fingerprint(approved, environment), fingerprint(current, environment)
    for get, put in ((lambda f: f["fields"]["items"], lambda f, v: f["fields"].__setitem__("items", v)),
                     (lambda f: f["wavelengths"], lambda f, v: f.__setitem__("wavelengths", v))):
        old, new = get(a), get(b)
        if len(new) > len(old) and new[:len(old)] == old:
            put(b, new[:len(old)])
    return diff(a, b)


def digest(fp: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(fp, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def materials(desc: dict[str, Any]) -> list[dict[str, Any]]:
    """逐面的介质及来源，记入每个调试轮次。 / Medium and source per surface, stored with every round."""
    out = []
    for s in desc["surfaces"]:
        src = s.get("material_source") or {}
        if src.get("kind") in ("model", "catalog"):
            item = {"surface": s["index"], "kind": src["kind"], "n": s.get("n")}
            item.update({"name": src.get("name"), "catalogs": src.get("catalogs"), "status": src.get("status")} if src["kind"] == "catalog"
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
        out.append({**record, "fingerprint": model["fingerprint"], "desc": model.get("model"), "environment": [int(i) for i in env]})
    return out


def require_approved(job: jobs.Job, desc: dict[str, Any]) -> dict[str, Any]:
    """当前镜头的结构必须与某个已确认的建模说明一致；否则报错并指出变了什么。按当前规则重新比较，旧的确认也适用。
    The current lens must match an approved model description; otherwise fail and say what changed.
    Comparison uses the current rule, so older approvals still count."""
    found = approvals(job)
    latest_changes: list[str] = []
    for record in reversed(found):
        env = record["environment"]
        if record.get("desc") is not None:
            changes = structure_changes(record["desc"], desc, env)
        else:
            # 早期的 model.json 没有存完整描述，只能比较当时的指纹 / early model.json files lack the description: compare the stored fingerprint
            changes = diff(record["fingerprint"], fingerprint(desc, env)) if digest(fingerprint(desc, env)) != record["fingerprint_sha"] else []
        if not changes:
            return {"model": record["model"], "fingerprint_sha": digest(fingerprint(desc, env)), "environment": env}
        if record is found[-1]:
            latest_changes = changes
    details: dict[str, Any] = {"fingerprint_sha": digest(fingerprint(desc))}
    if found:
        details["latest_approved"] = found[-1]["model"]
        details["changed"] = latest_changes[:30]
        message = ("the model changed since the last approved description (see details.changed); "
                   "run lens describe, show the page to the user and lens approve with their words before recording rounds")
    else:
        message = "no approved model description; run lens describe, show the page to the user and lens approve with their words before recording rounds"
    raise SaosError(ErrorCode.MODEL_NOT_APPROVED, message, details)
