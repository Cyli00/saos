from __future__ import annotations

import math

import pytest

from saos import prescription
from saos.protocol import SaosError


def minimal(**overrides):
    p = {
        "aperture": {"type": "object_na", "value": 0.4},
        "fields": {"type": "object_height", "y": [0, 0.5]},
        "wavelengths": {"um": [0.48, 0.546, 0.644], "primary": 2},
        "object": {"thickness": 13.3},
        "surfaces": [
            {"radius": -17.4, "thickness": 1.668, "material": "s-tim28"},
            {"radius": "inf", "thickness": 0, "comment": "saos:stop", "stop": True},
            {"type": "paraxial", "focal_length": 200, "thickness": 200, "comment": "saos:tube"},
        ],
    }
    p.update(overrides)
    return p


def test_normalize_defaults_and_materials():
    p = prescription.normalize(minimal())
    assert p["surfaces"][0]["material"] == "S-TIM28"
    assert math.isinf(p["surfaces"][1]["radius"])
    assert p["surfaces"][2]["opd_mode"] == 1
    assert p["object"]["comment"] == ""
    assert p["telecentric_object"] is False
    model = prescription.normalize_material({"model": {"nd": 1.5255, "vd": 56}}, "x")
    assert model == {"model": {"nd": 1.5255, "vd": 56.0, "dpgf": 0.0}}


@pytest.mark.parametrize(
    "change",
    [
        {"aperture": {"type": "epd", "value": 5}},
        {"fields": {"type": "angle", "y": [0]}},
        {"wavelengths": {"um": [0.5], "primary": 2}},
        {"surfaces": []},
        {"ray_aiming": "maybe"},
    ],
)
def test_normalize_rejects(change):
    with pytest.raises(SaosError):
        prescription.normalize(minimal(**change))


def test_two_stops_rejected():
    p = minimal()
    p["surfaces"][0]["stop"] = True
    with pytest.raises(SaosError):
        prescription.normalize(p)
