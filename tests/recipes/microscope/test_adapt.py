from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import pytest

from saos.recipes.microscope import adapt
from saos.protocol import SaosError

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "us4521083" / "extract.json").read_text(encoding="utf-8"))
CATALOGS = ["OHARA", "SCHOTT", "HOYA", "SUMITA"]


def test_schema1_fixture_still_adapts_identically():
    out = adapt.from_extraction(FIXTURE, catalogs=CATALOGS, focal_length_mm=10, field_number=27)
    par = out["report"]["paraxial"]
    assert par["efl_mm"] == pytest.approx(9.99441, abs=1e-4)
    assert par["front_focal_distance_mm"] == pytest.approx(13.3494, abs=1e-3)
    expect = out["expect"]
    assert {k: expect[k] for k in ("efl", "na", "rel_tol", "wd")} == {"efl": 10.0, "na": 0.4, "rel_tol": 0.01, "wd": 13.34}
    # 4 位小数的数据，舍入包络半宽远小于 1%
    assert expect["tolerance_mm"]["efl"] < 0.05 and expect["tolerance_mm"]["wd"] < 0.1
    assert out["prescription"]["telecentric_object"] is True
    assert out["report"]["elements"] == 10


def test_normalized_data_uses_beta_for_native_focal_length():
    out = adapt.from_extraction(FIXTURE, catalogs=CATALOGS, field_number=27)
    assert out["report"]["focal_length_mm"] == pytest.approx(10.0)  # 200 / |β|


def reversed_listing(ex2):
    """把物方到像方的表改写成从像方列起的表（与 oriented_rows 的倒序互逆）。"""
    rows = ex2["surfaces"]
    n = len(rows)
    out = []
    for k in range(n):
        src = rows[n - 1 - k]
        prev = rows[n - 2 - k] if n - 2 - k >= 0 else None
        row = {"i": k + 1, "r": -src["r"]}
        if prev is not None:
            row["d"] = prev["d"]
            if "n" in prev:
                row["n"], row["nu"] = prev["n"], prev["nu"]
        out.append(row)
    return {**ex2, "order": "image_to_object", "surfaces": out}


def test_image_to_object_tables_are_reversed():
    ex2 = adapt.upgrade(FIXTURE)
    back = adapt.oriented_rows(reversed_listing(ex2))
    for a, b in zip(ex2["surfaces"], back):
        assert b["r"] == pytest.approx(a["r"])
        assert b.get("n") == a.get("n")
        if a is not ex2["surfaces"][-1]:
            assert b["d"] == pytest.approx(a["d"])


def test_cover_glass_gap_uses_apparent_thickness():
    ex2 = adapt.upgrade(FIXTURE)
    ex2["cover_glass"] = {"thickness": 0.017, "n": 1.5255, "nu": 56.0}
    out = adapt.from_extraction(ex2, catalogs=CATALOGS, focal_length_mm=10, field_number=27)
    p = out["prescription"]
    ffd = out["report"]["paraxial"]["front_focal_distance_mm"]
    assert p["object"]["thickness"] == pytest.approx(0.17)
    assert p["surfaces"][0]["comment"] == "saos:cover_top"
    assert p["surfaces"][0]["thickness"] == pytest.approx(ffd - 0.17 / 1.5255)


@pytest.mark.parametrize("focal_length", [10.0, 20.0])
def test_assumed_cover_thickness_stays_in_millimeters(focal_length):
    ex = adapt.upgrade(copy.deepcopy(FIXTURE))
    ex["cover_glass"] = {"thickness": None, "n": None, "nu": None}
    before = copy.deepcopy(ex)
    out = adapt.from_extraction(ex, catalogs=CATALOGS, focal_length_mm=focal_length)
    p, report = out["prescription"], out["report"]
    assert p["object"]["thickness"] == pytest.approx(0.17)
    assert report["cover_glass"]["thickness"] * report["scale"] == pytest.approx(0.17)
    ffd = report["paraxial"]["front_focal_distance_mm"]
    assert p["surfaces"][0]["thickness"] == pytest.approx(ffd - 0.17 / 1.523)
    explicit = copy.deepcopy(ex)
    explicit["cover_glass"] = {"thickness": 0.17 / report["scale"], "n": 1.523, "nu": 55.0}
    reference = adapt.from_extraction(explicit, catalogs=CATALOGS, focal_length_mm=focal_length)
    assert report["rounding_envelope"] == reference["report"]["rounding_envelope"]
    assert ex == before


@pytest.mark.parametrize("cover", [
    {"thickness": 0.017, "n": None, "nu": 56.0},
    {"thickness": 0.017, "n": 1.5255, "nu": None},
])
def test_partial_cover_defaults_preserve_printed_values(cover):
    ex = adapt.upgrade(copy.deepcopy(FIXTURE))
    ex["cover_glass"] = cover
    out = adapt.from_extraction(ex, catalogs=CATALOGS, focal_length_mm=10)
    obj = out["prescription"]["object"]
    assert obj["thickness"] == pytest.approx(0.17)
    assert obj["material"]["model"]["nd"] == (cover["n"] or 1.523)
    assert obj["material"]["model"]["vd"] == (cover["nu"] or 55.0)


def test_check_and_adapt_use_the_same_assumed_cover():
    from saos.recipes.microscope.library_cli import check_extract

    ex = adapt.upgrade(copy.deepcopy(FIXTURE))
    ex["cover_glass"] = {"thickness": None, "n": 1.5255, "nu": 56.0}
    out = adapt.from_extraction(ex, catalogs=CATALOGS)
    check = next(c for c in check_extract(ex)["checks"] if c["quantity"] == "WD")
    scale = out["report"]["scale"]
    assert check["computed"] * scale == pytest.approx(out["prescription"]["surfaces"][0]["thickness"])
    assert [v * scale for v in check["envelope"]] == pytest.approx(out["report"]["rounding_envelope"]["wd"])


def test_check_printed_cover_does_not_require_native_scale():
    from saos.recipes.microscope.library_cli import check_extract

    ex = adapt.upgrade(copy.deepcopy(FIXTURE))
    ex["cover_glass"] = {"thickness": 0.017, "n": 1.5255, "nu": 56.0}
    with_beta = check_extract(ex)
    ex["header"].pop("beta")
    assert check_extract(ex) == with_beta


def test_printed_glass_name_is_used_when_consistent():
    ex2 = adapt.upgrade(copy.deepcopy(FIXTURE))
    ex2["surfaces"][8]["glass"] = "SF1"  # 1.71736 / 29.51
    ex2["surfaces"][0]["glass"] = "N-BK7"  # 与 1.5927 不符，应回退到按 nd/νd 匹配
    out = adapt.from_extraction(ex2, catalogs=CATALOGS, focal_length_mm=10, field_number=27)
    by = {(g["n"], g["printed"]): g for g in out["report"]["glasses"]}
    assert by[(1.71736, "SF1")]["how"] == "printed name"
    assert by[(1.5927, "N-BK7")]["how"].startswith("nearest in nd/νd")


def test_aspheric_surfaces_are_rejected():
    ex2 = adapt.upgrade(copy.deepcopy(FIXTURE))
    ex2["surfaces"][3]["aspheric"] = True
    with pytest.raises(SaosError):
        adapt.from_extraction(ex2, catalogs=CATALOGS, focal_length_mm=10)


def test_printed_stop_row_is_used():
    ex2 = adapt.upgrade(copy.deepcopy(FIXTURE))
    rows = ex2["surfaces"]
    rows.insert(10, {"i": "S", "r": None, "d": 0.0, "stop": True})
    out = adapt.from_extraction(ex2, catalogs=CATALOGS, focal_length_mm=10, field_number=27)
    p = out["prescription"]
    assert p["telecentric_object"] is False
    stops = [s for s in p["surfaces"] if s.get("stop")]
    assert len(stops) == 1 and math.isinf(stops[0]["radius"])
