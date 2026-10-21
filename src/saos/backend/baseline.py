"""基准量：一阶量、像面前间隔、各视场主光线落点、通光率与 RMS 光斑半径。只由 OpticStudio 阶段的复测与对比使用。"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from saos.protocol import SaosError


def normalized_fields(info: dict[str, Any]) -> list[tuple[float, float]]:
    items = info["fields"]["items"]
    if str(info["fields"].get("normalization", "Radial")).lower() == "rectangular":
        mx, my = max(abs(f["x"]) for f in items), max(abs(f["y"]) for f in items)
        return [(f["x"] / mx if mx else 0.0, f["y"] / my if my else 0.0) for f in items]
    r = max(math.hypot(f["x"], f["y"]) for f in items)
    return [(f["x"] / r if r else 0.0, f["y"] / r if r else 0.0) for f in items]


def baseline(backend) -> dict[str, Any]:
    """两端用同一套调用取基准量：一阶量、像面前间隔、各视场主波长的主光线落点、通光率与 RMS 光斑半径（主光线参考）。"""
    info = backend.system_info()
    primary = backend.primary_wave()
    image = len(backend.surfaces()) - 1
    out: dict[str, Any] = {"errors": {}}
    for name, int2 in (("EFFL", primary), ("EPDI", 0), ("ENPP", 0), ("EXPD", 0), ("EXPP", 0), ("TOTR", 0)):
        try:
            out[name] = float(backend.operand(name, 0, int2, []))
        except SaosError as exc:
            out[name] = None
            out["errors"][name] = exc.message
    out["working_distance"] = float(backend.surface_value(image - 1, "thickness"))
    grid = [(px, py) for px in np.linspace(-1, 1, 15) for py in np.linspace(-1, 1, 15) if px * px + py * py <= 1]
    fields = []
    for f, (hx, hy) in enumerate(normalized_fields(info), 1):
        chief = backend.trace_batch([(primary, hx, hy, 0.0, 0.0)], image)
        rays = backend.trace_batch([(primary, hx, hy, px, py) for px, py in grid], image)
        ok = rays.ok
        rms = float(np.sqrt(np.mean((rays.x[ok] - chief.x[0]) ** 2 + (rays.y[ok] - chief.y[0]) ** 2))) if chief.ok[0] and ok.any() else None
        fields.append({"field": f, "chief": [float(chief.x[0]), float(chief.y[0])] if chief.ok[0] else None,
                       "transmission": float(ok.mean()), "rms_radius": rms})
    out["fields"] = fields
    return out
