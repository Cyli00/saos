from __future__ import annotations

import pytest

from zea import library
from zea.spec import parse_spec


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setenv("ZEA_LIBRARY", str(tmp_path / "library"))
    return tmp_path / "library"


def make_entry(entry_id, na, f=10.0, status="admitted", eligible=True, claimed="plan"):
    return {
        "id": entry_id,
        "status": status,
        "metrics": {
            "focal_length_mm": f, "na": na, "field_number": 22.0, "elements": 8, "claimed_class": claimed,
            "cover_glass": None, "catalogs": ["OHARA"], "wd_mech_mm": 0.5 * f, "optical_length_mm": 4 * f, "clear_diameter_mm": 1.2 * f,
        },
        "benchmark": {"eligible": eligible},
    }


def test_entry_ids_are_restricted(lib):
    with pytest.raises(Exception):
        library.entry_dir("../x")


def test_split_is_deterministic_balanced_and_stratified(lib):
    entries = [make_entry(f"E{k}", na) for k, na in enumerate([0.1, 0.12, 0.25, 0.3, 0.4, 0.5, 0.6, 0.65])]
    entries.append(make_entry("REJ", 0.3, status="rejected"))
    entries.append(make_entry("NOTBENCH", 0.3, eligible=False))
    a = library.split_benchmarks(entries)
    b = library.split_benchmarks(list(reversed(entries)))
    assert a["groups"] == b["groups"]
    t, v = a["groups"]["tuning"], a["groups"]["validation"]
    assert len(t) + len(v) == 8 and abs(len(t) - len(v)) <= 1
    assert "REJ" not in t + v and "NOTBENCH" not in t + v
    for band in ("low", "mid", "high"):
        members = [c for c in a["cases"] if c["na_band"] == band]
        groups = {c["group"] for c in members}
        if len(members) >= 2:
            assert groups == {"tuning", "validation"}


def test_split_keeps_each_patent_in_one_group(lib):
    entries = []
    for patent, nas in {"P1": [0.3, 0.32, 0.35], "P2": [0.25, 0.4], "P3": [0.1, 0.5], "P4": [0.6]}.items():
        for k, na in enumerate(nas):
            e = make_entry(f"{patent}-e{k + 1}", na)
            e["source"] = {"patent": patent}
            entries.append(e)
    out = library.split_benchmarks(entries)
    by_patent = {}
    for c in out["cases"]:
        by_patent.setdefault(c["patent"], set()).add(c["group"])
    assert all(len(g) == 1 for g in by_patent.values())
    assert sum(c["patent"] == "P1" for c in out["cases"]) == library.MAX_PER_PATENT
    # P3 按最高 NA 0.5 归入高档，与 P4 同档时分进不同组
    assert by_patent["P3"] != by_patent["P4"]


def test_query_prefers_sufficient_na_and_scales_dimensions(lib):
    for e in (make_entry("LOWNA", 0.25), make_entry("MATCH", 0.42), make_entry("ACHRO", 0.45, claimed="achromat")):
        library.save_entry(e["id"], e)
    out = library.query(na=0.4, magnification=20, correction_class="plan_achromat")
    assert [m["id"] for m in out][0] == "MATCH"
    match = out[0]
    assert match["scaled"]["focal_length_mm"] == pytest.approx(10.0)
    assert match["scaled"]["working_distance_mech_mm"] == pytest.approx(5.0)
    low = next(m for m in out if m["id"] == "LOWNA")
    assert any("NA" in n for n in low["notes"])
    # 4×：焦距 50 mm，长度和口径按 5 倍放大
    big = library.query(na=0.4, magnification=4, correction_class="plan_achromat")[0]
    assert big["scaled"]["clear_diameter_mm"] == pytest.approx(60.0)


def probe_items(**over):
    base = {
        "magnification": (0.0, 0.05, True), "working_distance": (12.27, 0.01, True), "optical_length": (47.6, 1e6, True),
        "clear_diameter": (14.5, 1e6, True), "glass_status": (0, 0, True), "surface_types": (0, 0, True),
        "center_thickness_glass": (1.0, 0.001, True), "edge_thickness_glass": (0.2, 0.001, True), "air_gap": (0.11, 0, True),
        "axial_rms_wfe": (0.016, 0.0714, True), "axial_color": (1.3, 3.4, True), "osc": (0.0008, 0.0025, True),
        "plan_field": (-0.4, 0.0, True), "field_rms_wfe": (0.05, 0.0714, True), "lateral_color": (86.2, 16.7, False),
    }
    base.update(over)
    return {"items": [{"id": k, "value": v, "threshold": t, "pass": p} for k, (v, t, p) in base.items()]}


def test_derive_benchmark_spec_rules():
    entry = make_entry("X", 0.4)
    out = library.derive_benchmark_spec(entry, probe_items(), "plan_achromat")
    assert out["eligible"] is True
    limits = out["spec"]["limits"]
    assert limits["working_distance_min"] == pytest.approx(11.65)  # floor(12.27 × 0.95, 0.01)
    assert limits["optical_length_max"] == 53  # ceil(47.6 × 1.1)
    assert limits["max_clear_diameter"] == 16  # 默认值更大
    assert limits["min_edge_thickness_glass"] == pytest.approx(0.18)  # 0.2 × 0.9 < 默认 0.3
    assert out["spec"]["targets"]["lateral_color"]["threshold_um"] == pytest.approx(103.44)
    ids = {e["item"] for e in out["calibration_evidence"]}
    assert ids == {"lateral_color", "optical_length_max", "min_edge_thickness_glass"}
    parse_spec(out["spec"])  # 推导出的规格必须能被正常读取


def test_failing_fixed_item_makes_entry_ineligible():
    out = library.derive_benchmark_spec(make_entry("X", 0.4), probe_items(axial_color=(5.0, 3.4, False)), "plan_achromat")
    assert out["eligible"] is False and "axial_color" in out["failed_fixed_items"]


def test_negative_edge_air_gap_makes_entry_ineligible():
    # 透镜边缘相交是物理干涉，不能按实测值放宽
    out = library.derive_benchmark_spec(make_entry("X", 0.4), probe_items(air_gap=(-0.026, 0, False)), "plan_achromat")
    assert out["eligible"] is False and "air_gap" in out["failed_fixed_items"]


def test_every_subcommand_help_renders():
    import argparse

    from zea.cli import build_parser

    def walk(parser):
        parser.format_help()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    walk(child)

    walk(build_parser())
