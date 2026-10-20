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

判定的条件也在指纹中（2026-10-19 用户决定）：多重结构的结构数和每行操作数的类型与参数、TEMP 和 PRES 的值、环境温度气压、
切趾、偏振、膜层文件和无焦单位，改了就等于换了题目。多重结构里其余的值（变焦间隔等）和本设计要做的面的膜层是设计变量，
膜层逐轮记录；环境面的膜层固定。确认时的建模说明里还没有某项（这些项出现之前确认的）时，这一项不比较。
The conditions of the checks are in the fingerprint as well (user decision 2026-10-19): the number of configurations,
each operand row's type and parameters, the TEMP and PRES values, environment temperature and pressure, apodization,
polarization, the coating file and the afocal units; changing them changes the problem. Other multi-configuration
values (zoom spacings, …) and the coatings of surfaces this design makes are design variables, coatings recorded per
round; an environment surface's coating is fixed. An item missing from the approved description (approved before the
item existed) is not compared.

非序列系统（2026-10-20 加入）的指纹按物体：类型、参考物体、Inside Of 和介质类别。光源和探测器是判定的条件，
位置、倾斜和参数都在指纹里（光线数除外，那是采样）；环境物体（notes.environment 里的物体号）再加材料、参数、位置、
倾斜和体散射的全部设置；本设计要做的物体只记体散射的模型和 DLL。系统项另记波长、膜层文件和是否简单光线分裂。
A non-sequential system (added 2026-10-20) is fingerprinted per object: type, reference object, Inside Of and medium
class. Sources and detectors are conditions of the checks: position, tilt and parameters are included (ray counts are
sampling, not structure). Environment objects (object numbers in notes.environment) add material, parameters, position,
tilt and every volume-physics setting; objects this design makes keep only the volume-physics model and DLL.
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


# TEMP、PRES 的值是判定条件，不是设计变量 / TEMP and PRES values are conditions of the checks, not design variables
CONDITION_OPERANDS = ("TEMP", "PRES")
# 指纹里后来加的项：确认时的描述里没有就不比较 / items added to the fingerprint later: not compared when the approval lacks them
LATER_KEYS = ("configurations", "environment", "apodization", "polarization", "coating_file")


def _configurations(cfg: dict[str, Any]) -> dict[str, Any]:
    return {"count": cfg.get("count", 1),
            "operands": [[o["type"], o.get("params") or [], [_num(v) for v in o["values"]] if o["type"] in CONDITION_OPERANDS else None]
                         for o in cfg.get("operands") or []]}


def _medium_class_nsc(item: dict[str, Any]) -> str:
    kind = str((item.get("material_source") or {}).get("kind", "none"))
    return "glass" if kind in ("model", "catalog") else kind


def _fingerprint_objects(objects: list[dict[str, Any]], given: set[int], all_given: bool = False) -> list[dict[str, Any]]:
    """一组非序列物体的指纹；given 是锁定全部设置的物体号，all_given 时整组都锁（混合模式里作为环境的 NonSequential 面）。
    The fingerprint of one group of NSC objects; given are the fully locked objects, all_given locks the whole group."""
    from saos.backend.nsc import SAMPLING_HEADERS

    out = []
    for o in objects:
        locked = all_given or o["index"] in given
        item: dict[str, Any] = {"type": o["type"], "ref": o["ref"], "inside_of": o["inside_of"]}
        # 文件换了就是另一个零件或光源 / another file is another part or source
        item.update({k: str(o[k]).upper() for k in ("file", "file2") if o.get(k)})
        params = {p["name"]: _num(p["value"]) for p in o.get("params") or [] if str(p["name"]).strip().lower() not in SAMPLING_HEADERS}
        placement = {"position": [_num(v) for v in o["position"]], "tilt": [_num(v) for v in o["tilt"]]}
        vp = o.get("volume_physics")
        faces = o.get("faces") or []
        if o["kind"] != "object" or locked:
            # 条件和环境：面属性、光源页、Type 页、衍射页、折射率页全记 / conditions and environment: every tab
            item["faces"] = [{k: _deep(v) for k, v in f.items() if k != "name"} for f in faces]
            for key in ("scatter_to", "source", "type_options", "diffraction", "index_model"):
                if o.get(key):
                    item[key] = _deep(o[key])
        else:
            # 要设计的物体只记各面是否反射、吸收和散射模型，以及衍射、折射率的类型 / design objects: kinds only
            item["faces"] = [[f.get("is"), (f.get("scatter") or {}).get("model")] for f in faces]
            item["diffraction"] = (o.get("diffraction") or {}).get("split")
            item["index_model"] = (o.get("index_model") or {}).get("type")
        if o["kind"] != "object":
            item.update(params=params, **placement)
        elif locked:
            item.update(medium=str((o.get("material_source") or {}).get("kind", "none")), material=str(o.get("material", "")).upper(),
                        params=params, volume_physics=_deep(vp or {}), **placement)
        else:
            item.update(medium=_medium_class_nsc(o), volume_physics={k: (vp or {}).get(k) for k in ("model", "dll")})
        out.append(item)
    return out


def _deep(v: Any) -> Any:
    """嵌套结构里的数统一舍入。/ Round every number in a nested value."""
    if isinstance(v, dict):
        return {k: _deep(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_deep(x) for x in v]
    return _num(v)


def _fingerprint_nsc(desc: dict[str, Any], environment: Any = ()) -> dict[str, Any]:
    given = {int(i) for i in environment or ()}
    return {"mode": "non_sequential", "lens_units": desc.get("lens_units"),
            "wavelengths": [[_num(w["um"]), bool(w["primary"])] for w in desc["wavelengths"]],
            "coating_file": str(desc.get("coating_file") or "").upper(),
            "simple_ray_splitting": bool((desc.get("nsc") or {}).get("simple_ray_splitting")),
            "objects": _fingerprint_objects(desc["objects"], given)}


def fingerprint(desc: dict[str, Any], environment: Any = ()) -> dict[str, Any]:
    if desc.get("mode") == "non_sequential":
        return _fingerprint_nsc(desc, environment)
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
            if "coating" in s:
                item["coating"] = str(s["coating"]).upper()
        else:
            item.update(medium=medium_class(s), aperture={"type": aperture.get("type")}, tilted=_tilted(s))
            if "Fresnel" in str(s["type"]):
                # 平基底和弯基底是两种零件（实测光纤任务中基底被优化弯后穿透了板厚）
                # a flat and a curved substrate are different parts (in the fiber job an optimised substrate bent through the plate)
                item["substrate_flat"] = math.isinf(float(s["radius"]))
        if s.get("objects") is not None:
            # 混合模式：NonSequential 面的物体；面在环境里时整组锁定 / mixed mode: a NonSequential surface's objects
            item["objects"] = _fingerprint_objects(s["objects"], set(), all_given=s["index"] in given)
        surfaces.append(item)
    out = {
        "aperture": {k: _num(ap.get(k)) for k in ("type", "value", "telecentric_object", "afocal_image")},
        "ray_aiming": desc.get("ray_aiming"),
        "fields": {"type": desc["fields"]["type"], "normalization": desc["fields"].get("normalization"),
                   "items": [[_num(f["x"]), _num(f["y"])] for f in desc["fields"]["items"]]},
        "wavelengths": [[_num(w["um"]), bool(w["primary"])] for w in desc["wavelengths"]],
        "surfaces": surfaces,
    }
    if "afocal_units" in ap:
        out["aperture"]["afocal_units"] = ap["afocal_units"]
    if "configurations" in desc:
        out["configurations"] = _configurations(desc["configurations"])
    for key in ("environment", "apodization", "polarization"):
        if key in desc:
            out[key] = {k: [_num(x) for x in v] if isinstance(v, list) else _num(v) for k, v in desc[key].items()}
    if "coating_file" in desc:
        out["coating_file"] = str(desc["coating_file"]).upper()
    return out


def _align(approved: dict[str, Any], current: dict[str, Any]) -> None:
    """确认时还没有的指纹项从当前指纹里去掉，旧的确认照样适用。/ Drop from current what the approved fingerprint predates."""
    for key in LATER_KEYS:
        if key not in approved:
            current.pop(key, None)
    if "afocal_units" not in approved.get("aperture", {}):
        # 不是无焦时两边都没有这项；确认时无焦而没记单位的是旧描述 / absent when not afocal, or an old description
        current.get("aperture", {}).pop("afocal_units", None)
    for a, b in zip(approved.get("surfaces") or [], current.get("surfaces") or []):
        if "coating" not in a:
            b.pop("coating", None)


def structure_changes(approved: dict[str, Any], current: dict[str, Any], environment: Any = ()) -> list[str]:
    """已确认模型与当前镜头在结构上的不同之处；视场和波长在末尾追加的采样点不算。
    Structural differences between an approved model and the current lens; sampling points appended to fields or wavelengths do not count."""
    a, b = fingerprint(approved, environment), fingerprint(current, environment)
    if a.get("mode") != b.get("mode"):
        return ["mode"]
    _align(a, b)
    for get, put in ((lambda f: f["fields"]["items"], lambda f, v: f["fields"].__setitem__("items", v)),
                     (lambda f: f["wavelengths"], lambda f, v: f.__setitem__("wavelengths", v))):
        try:
            old, new = get(a), get(b)
        except KeyError:  # 非序列系统没有视场 / a non-sequential system has no fields
            continue
        if len(new) > len(old) and new[:len(old)] == old:
            put(b, new[:len(old)])
    return diff(a, b)


def digest(fp: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(fp, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# 多重结构里给模型玻璃赋值的操作数（折射率、阿贝数、部分色散）/ multi-configuration operands that set model-glass data
MODEL_GLASS_OPERANDS = ("MIND", "MABB", "MDPG")


def materials(desc: dict[str, Any]) -> list[dict[str, Any]]:
    """逐面的介质及来源，记入每个调试轮次；多重结构用 GLSS 换进的玻璃逐个结构列出（带 configuration）。
    Medium and source per surface, stored with every round; glasses a GLSS row swaps in are listed per configuration."""
    out = []
    for o in desc.get("objects") or []:
        src = o.get("material_source") or {}
        if o["kind"] == "object" and src.get("kind") in ("model", "catalog"):
            item = {"object": o["index"], "kind": src["kind"], "n": o.get("n")}
            item.update({"name": src.get("name"), "catalogs": src.get("catalogs"), "status": src.get("status")} if src["kind"] == "catalog"
                        else {"nd": src.get("nd"), "vd": src.get("vd")})
            out.append(item)
    for s in desc["surfaces"]:
        src = s.get("material_source") or {}
        if src.get("kind") in ("model", "catalog"):
            item = {"surface": s["index"], "kind": src["kind"], "n": s.get("n")}
            item.update({"name": src.get("name"), "catalogs": src.get("catalogs"), "status": src.get("status")} if src["kind"] == "catalog"
                        else {"nd": src.get("nd"), "vd": src.get("vd"), "dpgf": src.get("dpgf")})
            if src.get("approximation"):
                # Optiland 阶段的模型玻璃是 Abbe 近似 / an Optiland-stage model glass is an Abbe approximation
                item["approximation"] = src["approximation"]
            out.append(item)
    for op in (desc.get("configurations") or {}).get("operands") or []:
        if op["type"] == "GLSS" and op.get("params"):
            for c, (name, src) in enumerate(zip(op["values"], op.get("sources") or [{}] * len(op["values"])), start=1):
                if src.get("kind") == "catalog":
                    out.append({"surface": op["params"][0], "configuration": c, "kind": "catalog", "name": str(name).upper(),
                                "catalogs": src.get("catalogs"), "status": src.get("status")})
    return out


def model_glass_surfaces(desc: dict[str, Any]) -> list[int]:
    """用模型玻璃的面，含多重结构里设模型玻璃数据的面。/ Surfaces with a model glass, the multi-configuration ones included."""
    out = [s["index"] for s in desc["surfaces"] if medium_kind(s) == "model"]
    # 非序列系统按物体号 / a non-sequential system by object number
    out += [o["index"] for o in desc.get("objects") or [] if str((o.get("material_source") or {}).get("kind")) == "model"]
    for op in (desc.get("configurations") or {}).get("operands") or []:
        if op["type"] in MODEL_GLASS_OPERANDS and op.get("params") and op["params"][0] not in out:
            out.append(op["params"][0])
    return sorted(out)


def coatings(desc: dict[str, Any]) -> list[dict[str, Any]]:
    """逐面的膜层（设计变量，记入每个调试轮次）。/ Coatings per surface, a design variable stored with every round."""
    return [{"surface": s["index"], "coating": s["coating"]} for s in desc["surfaces"] if s.get("coating")]


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
            now = fingerprint(desc, env)
            _align(record["fingerprint"], now)
            changes = diff(record["fingerprint"], now) if digest(fingerprint(desc, env)) != record["fingerprint_sha"] else []
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
