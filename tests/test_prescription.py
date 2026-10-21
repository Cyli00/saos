from __future__ import annotations

import copy
import json
import math

import pytest

from saos import jobs, prescription
from saos.protocol import ErrorCode, SaosError


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
        {"fields": {"type": "angle", "y": [0, 95]}},
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


def test_asphere_headers_reach_optiland_without_changing_orders():
    from saos.prescription import canonical_surface_params
    from saos.backend.optiland_surfaces import geometry_kwargs
    params = {"par2":1.2e-5, "6th Order Term":-3e-8}
    assert canonical_surface_params("EvenAspheric",params) == {"par2":1.2e-5,"par3":-3e-8}
    _, geometry = geometry_kwargs({"type":"EvenAspheric","params":params})
    assert geometry["coefficients"] == [0,1.2e-5,-3e-8]
    with pytest.raises(SaosError):
        canonical_surface_params("EvenAspheric",{"par3":1,"6th Order Term":2})
    with pytest.raises(SaosError):
        canonical_surface_params("EvenAspheric",{"5th Order Term":1})



def test_odd_asphere_headers_map_to_odd_orders_and_unknown_headers_stay_visible():
    from saos.backend.optiland_surfaces import geometry_kwargs
    from saos.prescription import canonical_surface_params

    assert canonical_surface_params("OddAsphere", {"1st Order Term": 1.0, "8th Order Term": 2.0}) == {
        "par1": 1.0, "par8": 2.0}
    with pytest.raises(SaosError):
        canonical_surface_params("OddAsphere", {"par1": 1.0, "1st Order Term": 1.0})
    assert canonical_surface_params("OddAsphere", {"Odd Term": 1.0}) == {"Odd Term": 1.0}
    with pytest.raises(SaosError) as refused:
        geometry_kwargs({"type": "OddAsphere", "params": {"Odd Term": 1.0}})
    assert refused.value.code == ErrorCode.BACKEND_UNSUPPORTED


def test_snapshot_prescription_merges_equal_aliases_without_mutating_input():
    from saos.prescription import snapshot_prescription

    data = {"surfaces": [{"type": "EvenAspheric", "params": {"par2": 1e-5, "4th Order Term": 1e-5}}]}
    original = copy.deepcopy(data)
    out = snapshot_prescription(data)
    assert out["surfaces"][0]["params"] == {"par2": 1e-5}
    assert data == original

    conflict = {"surfaces": [{"type": "EvenAspheric", "params": {"par2": 1e-5, "4th Order Term": 2e-5}}]}
    with pytest.raises(SaosError) as refused:
        snapshot_prescription(conflict)
    assert refused.value.code == ErrorCode.BAD_ARGS
    assert refused.value.details == {"surface": "surfaces[0]", "phase": "snapshot_parameters"}


def test_snapshot_prescription_does_not_change_file_bytes(tmp_path):
    from saos.prescription import snapshot_prescription

    path = tmp_path / "prescription.json"
    path.write_text(json.dumps({"surfaces": [{"type": "EvenAspheric", "params": {"par2": 1e-5, "4th Order Term": 1e-5}}]}),
                    encoding="utf-8")
    before = jobs.sha256_file(path)
    snapshot_prescription(json.loads(path.read_text(encoding="utf-8")))
    assert jobs.sha256_file(path) == before
