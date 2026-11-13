"""saos backends: what rtc computes and which backend a lens or prescription lands on. No status here is written by hand:
what a lens may hold is asked of the rtc backend itself with minimal probe lenses, and operands and measurements follow
the rule provenance() uses, so the table cannot drift from what lens build, lens load and computed_by report.
"""

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path
from typing import Any, Callable

from saos import config, prescription
from saos.protocol import ErrorCode, SaosError

STATUS = {
    "reconciled": "rtc computes it and the numbers were compared with OpticStudio",
    "unreconciled": "rtc computes it from the manual's definition, not yet compared with OpticStudio; results list it in "
                    "computed_by (rounds: provenance) and the delivered round is re-measured on OpticStudio",
    "refused": "rtc answers BACKEND_UNSUPPORTED and gives no approximate number; a lens holding it computes on OpticStudio "
               "(backend_switch.stayed) and holds a licence while it does",
}

REFUSED_SHOWN = 12
MODEL_GLASS = {"model": {"nd": 1.5168, "vd": 64.17, "dpgf": 0}}


def _probe() -> dict[str, Any]:
    """The probe's base: a model-glass singlet with fixed semi-diameters and no unreconciled path on rtc, which _table checks."""
    return {"aperture": {"type": "entrance_pupil_diameter", "value": 10},
            "fields": {"type": "angle", "y": [0, 3]},
            "wavelengths": {"um": [0.5876], "weights": [1], "primary": 1},
            "ray_aiming": "off", "object": {"thickness": "inf"},
            "surfaces": [{"radius": 60, "thickness": 4, "material": copy.deepcopy(MODEL_GLASS), "stop": True, "semi_diameter": 6},
                         {"radius": -60, "thickness": 55, "semi_diameter": 6}],
            "image": {"semi_diameter": 6}}


def _finite(p: dict[str, Any]) -> None:
    p["object"]["thickness"] = 100


def _aperture(name: str) -> Callable[[dict[str, Any]], None]:
    def change(p):
        p["aperture"] = {"type": name} if name == "float_by_stop" else {"type": name, "value": 0.1 if name == "object_na" else 5}
        # Object-side apertures need a finite object
        if name in prescription.OBJECT_SIDE_APERTURES:
            _finite(p)
    return change


def _field(name: str) -> Callable[[dict[str, Any]], None]:
    def change(p):
        p["fields"]["type"] = name
        if name == "object_height":
            _finite(p)
    return change


def _aiming(name: str) -> Callable[[dict[str, Any]], None]:
    return lambda p: p.update(ray_aiming=name)


def _surface_type(name: str) -> Callable[[dict[str, Any]], None]:
    def change(p):
        if name == "paraxial":
            p["surfaces"] = [{"type": "paraxial", "focal_length": 50, "thickness": 50, "stop": True, "semi_diameter": 6}]
        else:
            p["surfaces"][0]["type"] = name
    return change


def _surface_aperture(name: str) -> Callable[[dict[str, Any]], None]:
    def change(p):
        shape = ({"min": 0, "max": 5} if name.startswith("circular") else {"arms": 3, "width": 0.2} if name == "spider"
                 else {"x_half": 4, "y_half": 4})
        p["surfaces"][1]["aperture"] = {"type": name, **shape}
    return change


def _solve(name: str) -> Callable[[dict[str, Any]], None]:
    solves = {"variable": {"radius": "variable"},
              "pickup": {"thickness": {"type": "pickup", "surface": 1}},
              "marginal_ray_height": {"thickness": {"type": "marginal_ray_height", "height": 0}},
              "f_number": {"radius": {"type": "f_number", "value": 5}}}
    return lambda p: p["surfaces"][1].update(solves=solves[name])


def _mirror(surface_type: str | None = None) -> Callable[[dict[str, Any]], None]:
    def change(p):
        p["surfaces"][1].update(material="MIRROR", thickness=-30)
        if surface_type:
            p["surfaces"][1]["type"] = surface_type
    return change


def _thermal(p: dict[str, Any]) -> None:
    p["environment"] = {"adjust_index": True}
    p["thermal"] = {"temperatures": [-20, 60], "housing_tce": 23.6}


def _zoom(p: dict[str, Any]) -> None:
    p["configurations"] = [{"operand": "THIC", "params": [2], "values": [55, 56]}]


def _telecentric(p: dict[str, Any]) -> None:
    _aperture("object_na")(p)
    _field("object_height")(p)
    p["telecentric_object"] = True


# The probes: group → item → the change to the base lens. Items are named as in prescriptions; other is any unlisted type.
def _probes() -> dict[str, dict[str, Callable[[dict[str, Any]], None]]]:
    from saos.backend import rtc_lens

    types = {}
    for name, code in rtc_lens.TYPES.items():
        types.setdefault(code, name)
    return {
        "aperture": {name: _aperture(name) for name in prescription.APERTURES},
        "fields": {name: _field(name) for name in prescription.FIELDS},
        "ray_aiming": {name: _aiming(name) for name in prescription.RAY_AIMING},
        "surface_type": {**{name: _surface_type(name) for name in types.values()},
                         "coordinatebreak": _surface_type("CoordinateBreak"), "other": _surface_type("OddAsphere")},
        "surface_aperture": {name: _surface_aperture(name) for name in prescription.SURFACE_APERTURES},
        "solve": {name: _solve(name) for name in prescription.SOLVES},
        "lens": {
            "finite_object": _finite,
            "automatic_semi_diameter": lambda p: p["surfaces"][1].pop("semi_diameter"),
            "mirror": _mirror(),
            "mirror_on_evenaspheric": _mirror("EvenAspheric"),
            "decenter_tilt": lambda p: p["surfaces"][0].update(decenter_tilt={"before": {"tilt_x": 5}, "after": "reverse"}),
            "configurations_thickness": _zoom,
            "thermal": _thermal,
            "telecentric_object": _telecentric,
            "afocal": lambda p: p.update(afocal=True),
            "apodization": lambda p: p.update(apodization={"type": "gaussian", "factor": 1}),
            "polarization": lambda p: p.update(polarization={"unpolarized": False, "jx": 1, "jy": 0}),
            "units_other_than_mm": lambda p: p.update(units="in"),
        },
    }


def _rtc(glass_dir: Path | None = None):
    from saos.backend.rtc import RtcBackend

    return RtcBackend(None, glass_dir=glass_dir, standalone=True)


def _refusal(exc: SaosError) -> list[str]:
    if exc.code != ErrorCode.BACKEND_UNSUPPORTED:
        raise exc
    return list((exc.details or {}).get("problems") or [exc.message])


def _entry(status: str, why: list[str]) -> dict[str, Any]:
    return {"status": status, "why": why} if why else {"status": status}


def _built(raw: dict[str, Any]):
    backend = _rtc()
    backend.build(prescription.normalize(raw))
    return backend


def _ask(change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    raw = _probe()
    change(raw)
    try:
        backend = _built(raw)
    except SaosError as exc:
        return _entry("refused", _refusal(exc))
    why = backend.unverified()
    return _entry("unreconciled" if why else "reconciled", why)


def _measured(backend, call: Callable[[Any], Any]) -> dict[str, Any]:
    """Classify one call the way provenance() does."""
    backend.provenance()
    try:
        call(backend)
    except SaosError as exc:
        return _entry("refused", _refusal(exc))
    return _entry("unreconciled" if backend.provenance()["operands_unverified"] else "reconciled", [])


def _by_status(names, reconciled) -> dict[str, list[str]]:
    return {"reconciled": sorted(n for n in names if n in reconciled), "unreconciled": sorted(n for n in names if n not in reconciled)}


def table() -> dict[str, Any]:
    """rtc's capability table. OpticStudio computes every item through the ZOS-API and its numbers are the reference."""
    from saos import daemon
    from saos.backend import rtc, rtc_merit

    base = _built(_probe())
    if base.unverified():
        # Every probe would inherit the base's paths
        raise SaosError(ErrorCode.INTERNAL_ERROR, f"the capability probe lens is no longer fully reconciled on rtc: {base.unverified()}")
    with tempfile.TemporaryDirectory() as folder:
        analyses = {name: _measured(base, lambda b, n=name: b.analysis(n, {}, Path(folder) / "analysis.txt")) for name in base.analysis_types()}
        analyses["other"] = _measured(base, lambda b: b.analysis("HuygensPsf", {}, Path(folder) / "analysis.txt"))
    computed = rtc.RAY_OPERANDS | rtc.SYSTEM_OPERANDS
    return {
        "status": STATUS,
        "zos": "OpticStudio computes every item below through the ZOS-API, and its numbers are the reference",
        "rtc": {
            **{group: {name: _ask(change) for name, change in items.items()} for group, items in _probes().items()},
            "configuration_operands": sorted(rtc.THERMAL_OPS),
            "settings_carried_unread": list(rtc.UNREAD_SETTINGS),
            "operands": {**_by_status(computed, rtc.RECONCILED_OPERANDS), "other": "refused"},
            "merit_operands": {**_by_status(rtc_merit.merit_operands() - rtc_merit.INERT, rtc.RECONCILED_OPERANDS), "other": "refused"},
            "measurements": {
                "seidel": _measured(base, lambda b: b.seidel()),
                "wavefront_rms": _measured(base, lambda b: b.wavefront_rms(1, [1])),
                "huygens": _measured(base, lambda b: b.huygens(1, 32, 32)),
            },
            "analyses": analyses,
        },
        "always_zos": {"operations": sorted(daemon.OPTICSTUDIO_ONLY), "scan_plans": list(daemon.OPTICSTUDIO_SCANS),
                       "stages": sorted(stage for stage, backend in daemon.STAGE_BACKENDS.items() if backend == "zos")},
        "notes": [
            "configuration_operands: multi-configuration rows rtc reads from a .zmx; any other row is refused.",
            "settings_carried_unread: saved back unchanged and never applied by rtc; they are checked when the round is "
            "re-measured on OpticStudio.",
            "An item's why lists every path its probe lens takes, so object_na and object_height also show the finite object "
            "they require.",
            "Ask about a real lens with --lens or --prescription: combinations (a mirror with real ray aiming, a surface type "
            "inside multi-configuration rows, a catalog glass) are judged on the lens, not in this table.",
        ],
    }


def _verdict(source: dict[str, Any], load: Callable[[Any], None]) -> dict[str, Any]:
    from saos.backend import rtc_merit

    backend = _rtc(config.glass_dir())
    try:
        load(backend)
    except SaosError as exc:
        refused = _refusal(exc)
        # An NSC object brings a dozen unknown lines each
        more = len(refused) - REFUSED_SHOWN
        return {**source, "backend": "zos", "refused": refused[:REFUSED_SHOWN] + ([f"and {more} more"] if more > 0 else [])}
    known = rtc_merit.merit_operands()
    return {**source, "backend": "rtc", "unreconciled": backend.unverified(), "carried_unread": backend.carried_unread(),
            "merit_refused": sorted({row.type for row in backend.merit if row.type not in known})}


def lens(path: Path) -> dict[str, Any]:
    """Which backend a .zmx computes on: rtc with its unreconciled paths and the merit operands rtc lacks, or zos with the reasons."""
    path = Path(path)
    if not path.is_file():
        raise SaosError(ErrorCode.FILE_NOT_FOUND, f"lens file not found: {path}")
    return _verdict({"lens": str(path)}, lambda backend: backend.load(path))


def from_prescription(path: Path) -> dict[str, Any]:
    """As lens(), for a lens build prescription."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SaosError(ErrorCode.FILE_NOT_FOUND, f"cannot read {path}: {exc}") from exc
    except ValueError as exc:
        raise SaosError(ErrorCode.BAD_ARGS, f"{path} is not JSON: {exc}") from exc
    normalized = prescription.normalize(raw)
    return _verdict({"prescription": str(path)}, lambda backend: backend.build(normalized))
