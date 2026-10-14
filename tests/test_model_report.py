"""建模说明：自动检查规则、页面内容和 lens.describe 的存档位置（假后端，不需要 OpticStudio）。"""

from __future__ import annotations

import math

from saos import jobs, model_report
from saos.backend.fake import FakeBackend
from saos.daemon import Daemon

AIR = {"kind": "none"}
BK7 = {"kind": "catalog", "name": "N-BK7", "catalogs": ["SCHOTT"]}
MODEL = {"kind": "model", "nd": 1.6, "vd": 50.0, "dpgf": 0.0}


def build(rows, telecentric=False, fields=((0.0, 0.0), (0.0, 0.2))):
    """与 backend.describe_model 相同的结构。rows：(面型, 半径, 圆锥, 材料来源, 各波长折射率)。"""
    surfaces = [
        {"index": k, "type": kind, "comment": f"s{k}", "radius": r, "conic": c, "thickness": 0.1,
         "material": src.get("name", ""), "is_stop": k == 1, "material_source": src, "n": n,
         "aperture": {"type": "None"}, "solve_detail": {}, "params": [], "global_vertex": [0.0, 0.0, 0.1 * k]}
        for k, (kind, r, c, src, n) in enumerate(rows)
    ]
    return {
        "lens_units": "Millimeters",
        "aperture": {"type": "ObjectSpaceNA", "value": 0.5, "telecentric_object": telecentric, "afocal_image": False},
        "ray_aiming": "Off",
        "fields": {"type": "ObjectHeight", "normalization": "Radial",
                   "items": [{"x": x, "y": y, "weight": 1.0, "vignetting": dict.fromkeys(("VDX", "VDY", "VCX", "VCY", "VAN"), 0.0)} for x, y in fields]},
        "wavelengths": [{"um": 0.488, "weight": 1.0, "primary": False}, {"um": 0.532, "weight": 1.0, "primary": True}],
        "material_catalogs": ["SCHOTT"],
        "stop_surface": 1,
        "surfaces": surfaces,
        "layout": {"3d": "layout-3d.png", "cross_section": {"error": "boom"}},
    }


ALL = {"surfaces": {str(k): f"part {k}" for k in range(5)}, "first_order": ["m = 0.75"]}


def codes(d, notes=ALL):
    return [c for c, _ in model_report.checks(d, notes)]


def test_clean_model_has_no_warnings():
    d = build([("Standard", math.inf, 0.0, AIR, [1.0, 1.0]), ("Standard", 10.0, 0.0, BK7, [1.5222, 1.5195]),
               ("Standard", -10.0, 0.0, AIR, [1.0, 1.0]), ("Standard", math.inf, 0.0, AIR, [1.0, 1.0])], telecentric=True)
    assert codes(d) == []


def test_each_rule_fires_on_the_fiber_mistakes():
    d = build([
        ("Standard", math.inf, 0.0, {"kind": "model", "nd": 1.49, "vd": 1e8, "dpgf": 0.0}, [1.49, 1.49]),  # 无色散模型介质
        ("Standard", math.inf, 0.0, MODEL, [1.608, 1.604]),
        ("Generalized Fresnel", 0.0211, -5.7, AIR, [1.0, 1.0]),                                          # 弯曲的菲涅尔基底
        ("Standard", math.inf, 0.0, {"kind": "catalog", "name": "XYZ", "catalogs": []}, [1.5, 1.49]),
        ("Standard", math.inf, 0.0, AIR, [1.0, 1.0]),
    ])
    found = model_report.checks(d, {"surfaces": {"0": "fiber face"}})
    got = [c for c, _ in found]
    assert got.count("model_material") == 2
    assert {"no_dispersion", "curved_fresnel", "uncatalogued", "non_telecentric", "undescribed", "no_first_order"} <= set(got)
    assert dict(found)["undescribed"]["list"] == "1, 2, 3, 4"
    # 平板菲涅尔（半径无穷、圆锥 0）不报
    flat = build([("Standard", math.inf, 0.0, AIR, [1.0, 1.0]), ("Generalized Fresnel", math.inf, 0.0, AIR, [1.0, 1.0])], telecentric=True)
    assert codes(flat) == []


def test_telecentric_rule_needs_object_aperture_and_off_axis_field():
    rows = [("Standard", math.inf, 0.0, AIR, [1.0, 1.0]), ("Standard", math.inf, 0.0, AIR, [1.0, 1.0])]
    assert "non_telecentric" not in codes(build(rows, fields=((0.0, 0.0),)))
    d = build(rows)
    d["aperture"]["type"] = "EntrancePupilDiameter"
    assert "non_telecentric" not in codes(d)


def test_page_lists_materials_indices_fields_and_layout_both_languages():
    d = build([("Standard", math.inf, 0.0, AIR, [1.0, 1.0]), ("Standard", 10.0, 0.0, BK7, [1.522242, 1.519473]),
               ("Standard", math.inf, 0.0, MODEL, [1.608188, 1.603981]), ("Standard", math.inf, 0.0, AIR, [1.0, 1.0])])
    meta = {"job": "j", "lens": "a.zmx", "time": "t"}
    zh = model_report.render(d, {"surfaces": {"1": "棱镜入射面"}, "assumptions": ["斜面按理想反射"], "first_order": ["倍率 0.75"]}, meta, "zh")
    for text in ("一阶分析", "倍率 0.75", "N-BK7", "目录 SCHOTT", "1.522242", "1.519473", "模型玻璃（不是真实材料）", "n(488 nm)", "n(532 nm)",
                 "ObjectSpaceNA = 0.5", "棱镜入射面", "斜面按理想反射", "layout-3d.png", "没有生成：boom", "需要注意"):
        assert text in zh, text
    en = model_report.render(d, None, meta, "en")
    assert "Model glass (not a real material)" in en and "Check these" in en


def test_describe_op_archives_page_json_and_returns_warnings(saos_home, fake_lens_file):
    backend = FakeBackend()
    daemon = Daemon(backend, backend.start(), "standalone")
    job = jobs.create_job("a")
    for op, args in (("lens.import", {"src": str(fake_lens_file), "name": "start.zmx"}), ("lens.load", {"file": "start.zmx"})):
        assert daemon.handle({"op": op, "args": {"job": job.id, **args}})["ok"]
    res = daemon.handle({"op": "lens.describe", "args": {"job": job.id, "lang": "en", "notes": {"surfaces": {"0": "object"}}}})
    assert res["ok"], res
    out = res["result"]
    assert out["folder"] == "models/001-start"
    folder = job.root / out["folder"]
    assert (folder / "index.html").is_file() and (folder / "model.json").is_file()
    assert any(w.startswith("These surfaces have no description") for w in out["warnings"])
    again = daemon.handle({"op": "lens.describe", "args": {"job": job.id}})["result"]
    assert again["folder"] == "models/002-start"


def test_material_record_warnings_and_validation(saos_home):
    """材料来源记录 / the material source record."""
    import pytest
    import yaml

    from saos import materials_record
    from saos.protocol import SaosError

    d = build([("Standard", math.inf, 0.0, AIR, [1.0, 1.0]), ("Standard", 10.0, 0.0, BK7, [1.5222, 1.5195]),
               ("Standard", math.inf, 0.0, AIR, [1.0, 1.0])], telecentric=True)
    notes = {**ALL}
    assert [c for c, _ in model_report.checks(d, notes, None)] == ["no_material_record"]
    assert [c for c, _ in model_report.checks(d, notes, set())] == ["unrecorded_material"]
    assert model_report.checks(d, notes, {"N-BK7"}) == []
    job = jobs.create_job("mat")
    assert materials_record.recorded_names(job) is None
    record = {"schema": 1, "sources": {"tds": {"url": "https://example.org/tds.pdf"}},
              "materials": [{"id": "prism", "zemax": {"catalog": "SCHOTT", "name": "N-BK7"}},
                            {"id": "glue", "zemax": {"name": "GLUE1"}, "source_ids": ["tds"]}]}
    (job.root / materials_record.FILE).write_text(yaml.safe_dump(record), encoding="utf-8")
    assert materials_record.recorded_names(job) == {"N-BK7", "GLUE1"}
    for broken in ({**record, "schema": 2},
                   {**record, "materials": [{"id": "x", "zemax": {"name": "X"}}]},                       # 没有来源 / no source
                   {**record, "materials": [{"id": "x", "zemax": {"name": "X"}, "source_ids": ["nope"]}]}):  # 来源不存在
        (job.root / materials_record.FILE).write_text(yaml.safe_dump(broken), encoding="utf-8")
        with pytest.raises(SaosError):
            materials_record.recorded_names(job)


def test_fingerprint_catches_layout_changes():
    """折转角、偏心、菲涅尔基底变化都算结构改变；半径不算。 / Fold angles, decenters and Fresnel substrates are structure; radii are not."""
    from saos import model_gate

    d = build([("Standard", math.inf, 0.0, AIR, [1.0, 1.0]), ("Coordinate Break", math.inf, 0.0, {"kind": "inherit"}, [1.5, 1.5]),
               ("Generalized Fresnel", math.inf, 0.0, AIR, [1.0, 1.0]), ("Standard", 20.0, 0.0, BK7, [1.52, 1.51])], telecentric=True)
    d["surfaces"][1]["params"] = [{"par": 3, "name": "Tilt About Y", "value": 45.0, "solve": "Fixed"}]
    base = model_gate.digest(model_gate.fingerprint(d))

    def changed(mutate):
        import copy
        x = copy.deepcopy(d)
        mutate(x)
        return model_gate.digest(model_gate.fingerprint(x)) != base

    assert changed(lambda x: x["surfaces"][1]["params"][0].update(value=44.0))
    assert changed(lambda x: x["surfaces"][1].update(thickness=0.2))
    assert changed(lambda x: x["surfaces"][2].update(radius=0.02))
    assert changed(lambda x: x["surfaces"][3].update(tilt_decenter={"before_DecenterY": 0.01}))
    assert not changed(lambda x: x["surfaces"][3].update(radius=25.0))
    assert not changed(lambda x: x["surfaces"][3]["material_source"].update(name="SF2"))
