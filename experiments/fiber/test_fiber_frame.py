"""光纤侧视框架的实验测试；不在默认 testpaths 中，运行：uv run pytest experiments/fiber"""

from __future__ import annotations

import os

import pytest

import fiber_frame
from zea.backend.zos import ZosBackend
from zea.protocol import ZeaError, jsonable

zemax = pytest.mark.skipif(os.getenv("ZEA_RUN_ZEMAX") != "1", reason="需要显式启用真实 Zemax")


def test_geometry_uses_global_distance_and_detects_wrong_fold():
    rows = [{"type": "Standard", "material": ""} for _ in range(11)]
    rows[3]["material"] = "MIRROR"
    rows[8]["type"] = rows[9]["type"] = "Biconic"
    positions = [
        [0.205, 0, 0.35],
        [0.20, 0, 0.35],
        [0, 0, 0.35],
        [0, 0, 0.35],
        [0, 0, 0.35],
        [0, 0, 0.15],
        [0, 0, 0.14],
        [0, 0, 0.06],
        [0, 0, 0.05],
        [0, 0, 0],
        [0, 0, -0.1],
    ]
    snapshot = {"rows": rows, "positions_mm": positions}
    assert fiber_frame.geometry_checks(snapshot)["pass"]
    positions[10] = [0.1, 0, 0]
    assert not fiber_frame.geometry_checks(snapshot)["pass"]


def test_invalid_medium_is_rejected_before_build():
    with pytest.raises(ZeaError):
        fiber_frame.build(None, "oil")


def test_hemiellipsoid_sag_returns_to_bond_plane_at_equator():
    import math

    for diameter in (0.106, 0.134, 0.4):
        shape = fiber_frame.hemiellipsoid(diameter, 0.09)
        b, a = diameter / 2, 0.09
        for fraction in (0, 0.5, 0.99, 1):
            r = b * fraction
            radius, k = shape["vertex_radius_mm"], shape["conic"]
            sag = (r * r / radius) / (
                1 + math.sqrt(max(0, 1 - (1 + k) * r * r / (radius * radius)))
            )
            assert sag == pytest.approx(
                a * (1 - math.sqrt(1 - fraction * fraction)), abs=2e-9
            )


@pytest.mark.parametrize(
    "diameter,height", [(0.501, 0.09), (0.4, 0.1), (0.4, 0), (float("nan"), 0.09)]
)
def test_invalid_ellipsoid_rejected_before_build(diameter, height):
    with pytest.raises(ZeaError):
        fiber_frame.build(None, "air", diameter, height)


@pytest.mark.zemax
@zemax
def test_folded_fiber_framework_air_water_reload(tmp_path):
    b = ZosBackend()
    try:
        b.start()
        for medium in ("air", "water"):
            result = fiber_frame.build(b, medium)
            assert result["num_surfaces"] == 11
            before = fiber_frame.inspect_model(b)
            lde = b.system.LDE
            assert lde.GetSurfaceAt(5).Thickness == 0
            assert lde.GetSurfaceAt(5).Material == "N-BK7"
            assert lde.GetSurfaceAt(6).Radius == float("inf")
            assert lde.GetSurfaceAt(6).Thickness == -0.09
            assert lde.GetSurfaceAt(7).Conic == pytest.approx(
                result["lens_shape"]["conic"]
            )
            probe = fiber_frame.focus_probe(b)
            assert probe["all_forward_converging"]
            assert all(
                abs(row["target_offset_um"]) < 30 for row in probe["measurements"]
            )
            assert fiber_frame.geometry_checks(before)["pass"]
            assert before["system"]["aperture"]["value"] == 0.5
            assert len(before["system"]["fields"]["items"]) == 9
            assert [w["um"] for w in before["system"]["wavelengths"]] == [0.488, 0.532]
            file = tmp_path / f"{medium}.zmx"
            b.save_as(file)
            b.load(file)
            assert jsonable(fiber_frame.inspect_model(b)) == jsonable(before)
            assert b.trace_check([(w, 0.0, 0.0, 0.0, 0.0) for w in (1, 2)]) == []
        fiber_frame.build(b, "air")
        lde.GetSurfaceAt(5).Thickness = -0.01
        lde.GetSurfaceAt(5).Material = ""
        lde.GetSurfaceAt(6).Radius = -0.6
        lde.GetSurfaceAt(6).Thickness = -0.08
        lde.GetSurfaceAt(7).Radius = 0.6
        lde.GetSurfaceAt(7).Conic = 0
        assert not fiber_frame.focus_probe(b)["all_forward_converging"]
    finally:
        b.close()
