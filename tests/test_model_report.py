"""建模说明：自动检查规则、页面内容和 lens.describe 的存档位置（假后端，不需要 OpticStudio）。"""

from __future__ import annotations

import copy
import json
import math

import pytest
import yaml

from saos import demand, jobs, materials_record, model_gate, model_report
from saos.backend.fake import FakeBackend
from saos.daemon import Daemon
from saos.protocol import ErrorCode, SaosError

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


def test_obsolete_catalog_glass_is_flagged_and_its_status_shown():
    """玻璃替换常换成停产牌号：目录状态为 Obsolete 时提示，来源一栏写出状态。"""
    sk16 = {"kind": "catalog", "name": "SK16", "catalogs": ["SCHOTT"], "status": "Obsolete"}
    d = build([("Standard", math.inf, 0.0, AIR, [1.0, 1.0]), ("Standard", 10.0, 0.0, sk16, [1.6239, 1.6216]),
               ("Standard", -10.0, 0.0, {**BK7, "status": "Preferred"}, [1.5222, 1.5195]),
               ("Standard", math.inf, 0.0, AIR, [1.0, 1.0])], telecentric=True)
    found = model_report.checks(d, ALL)
    assert [c for c, _ in found] == ["obsolete_material"] and dict(found)["obsolete_material"] == {"s": 1, "name": "SK16"}
    text, _ = model_report._source_text(sk16, model_report.Tr("en"))
    assert text == "catalog SCHOTT, Obsolete"
    assert codes(d, {**ALL, "environment": [1]}) == []  # 外购件已经存在，不提示 / a bought-in part already exists


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
    daemon = Daemon(backend, backend.start(), "extension")
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


def test_fingerprint_keeps_part_kinds_and_the_environment():
    """本设计要做的面：倾角、坐标断点参数、半径和材料都是设计变量；基底由平变弯、新增倾斜才算改结构。
    声明为环境的面（外购件、样品）再锁定参数。视场和波长可以在末尾追加采样点。
    Surfaces this design makes: tilt angles, break parameters, radii and materials are variables; a flat substrate turning
    curved or a new tilt is structure. Environment surfaces are locked further. Fields and wavelengths may gain points at the end."""
    import copy

    from saos import model_gate

    d = build([("Standard", math.inf, 0.0, AIR, [1.0, 1.0]), ("Coordinate Break", math.inf, 0.0, {"kind": "inherit"}, [1.5, 1.5]),
               ("Generalized Fresnel", math.inf, 0.0, AIR, [1.0, 1.0]), ("Standard", 20.0, 0.0, BK7, [1.52, 1.51])], telecentric=True)
    d["surfaces"][1]["params"] = [{"par": 3, "name": "Tilt About Y", "value": 45.0, "solve": "Fixed"}]

    def changed(mutate, environment=()):
        x = copy.deepcopy(d)
        mutate(x)
        return model_gate.structure_changes(d, x, environment)

    assert not changed(lambda x: x["surfaces"][1]["params"][0].update(value=44.0))   # 折转角是设计变量 / a fold angle is a variable
    assert not changed(lambda x: x["surfaces"][1].update(thickness=0.2))
    assert changed(lambda x: x["surfaces"][2].update(radius=0.02)) == ["surfaces[2].substrate_flat"]
    assert changed(lambda x: x["surfaces"][3].update(tilt_decenter={"before_DecenterY": 0.01})) == ["surfaces[3].tilted"]
    assert changed(lambda x: x["surfaces"][1]["params"][0].update(value=0.0)) == ["surfaces[1].tilted"]   # 去掉折转 / the fold removed
    assert not changed(lambda x: x["surfaces"][3].update(radius=25.0))
    assert not changed(lambda x: x["surfaces"][3]["material_source"].update(name="SF2"))
    assert not changed(lambda x: x["surfaces"][3].update(material_source={"kind": "model", "nd": 1.6, "vd": 50.0}))
    assert changed(lambda x: x["surfaces"][3].update(material_source={"kind": "mirror"})) == ["surfaces[3].medium"]
    # 外购的折转棱镜声明为环境：折转角和材料都锁定 / a bought-in fold declared as environment: angle and material are locked
    assert changed(lambda x: x["surfaces"][1]["params"][0].update(value=44.0), environment=[1]) == ["surfaces[1].break.Tilt About Y"]
    assert changed(lambda x: x["surfaces"][3].update(material="SF2", radius=25.0), environment=[3]) == ["surfaces[3].material", "surfaces[3].radius"]
    # 视场、波长只能在末尾追加 / fields and wavelengths only gain points at the end
    assert not changed(lambda x: x["fields"]["items"].append({"x": 0.0, "y": 0.5, "weight": 1.0}))
    assert changed(lambda x: x["fields"]["items"][0].update(y=0.1)) == ["fields.items[0][1]"]
    assert not changed(lambda x: x["wavelengths"].append({"um": 0.65, "weight": 1.0, "primary": False}))


# ---------- 确认页的四节（选型依据、需求逐项、计算有效性、下一阶段预算） ----------

DEMAND_DOC = {"schema": 2, "constants": [{"id": "lens", "description": "样品单片镜", "value": "a.zmx", "source": "user"}],
              "variables": [],
              "objectives": [{"id": "length", "description": "总长", "check": {"operand": {"type": "TOTR"}}, "origin": "user",
                              "must": {"max": 150.0}, "scale": 5.0, "source": "user"}],
              "summary": "总长不超过 150 mm。发现任何这个demand可能存在的问题，要大胆质疑和询问。"}
BUDGET = {"max_seconds": 900, "plan": "对选定结构做 Hammer，再扫描间隔三点"}


def _write_demand(job, doc=DEMAND_DOC):
    path = job.root / demand.DEMAND_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    demand._PREPARED.clear()


def _shown_fixture():
    """一份手写的四节数据，覆盖选型的各种结论、三种状态、材料的未知级别和预算。
    Hand-made four-section data covering every verdict, the three statuses, unknown material levels and a budget."""
    rows = [
        {"id": "length", "section": "objectives", "description": "总长", "origin": "user", "bound": {"max": 150.0}, "value": 100.0,
         "status": "met", "reason": None, "detail": None, "source": "user", "assumed": False},
        {"id": "spot", "section": "objectives", "description": "点列", "origin": "agent", "bound": {"max": 5.0}, "value": 7.0,
         "status": "unmet", "reason": None, "detail": None, "source": "agent assumption: 经验值", "assumed": True},
        {"id": "err", "section": "objectives", "description": "出错的判定", "origin": "user", "bound": {"max": 1.0}, "value": None,
         "status": "unknown", "reason": "check_error", "detail": "boom", "source": "user", "assumed": False},
    ]
    materials = [
        {"name": "N-BK7", "kind": "catalog", "where": ["surface 1"],
         "levels": {"found": {"state": "yes", "note": {"code": "in_record", "id": "prism", "source": {"kind": "vendor", "catalog": "SCHOTT"}}},
                    "complete": {"state": "unknown", "note": {"code": "no_dispersion"}},
                    "backend": {"state": "yes", "note": {"code": "catalog_in_use", "catalogs": ["SCHOTT"]}},
                    "loaded": {"state": "yes", "note": {"code": "measured", "n": 2}}}},
    ]
    rows_choice = [
        {"id": "a", "verdict": "valid", "form": "分组缩短总长", "provenance": {"book": "自动化测试夹具来源"},
         "prediction": {"first_order": {"efl": 50}}, "numeric_warnings": {"count": 2, "first": "RuntimeWarning: x"}},
        {"id": "b", "verdict": "invalid", "problems": ["两片穿插"]},
        {"id": "c", "verdict": "material", "error": "not in catalog"},
    ]
    selection = {"mode": "free", "chosen": {**rows_choice[0], "candidate": "a", "scan": "scans/001",
                 "link": "../../scans/001/index.html"}, "matches": ["a"], "rows": rows_choice}
    return {"selection": selection,
            "requirements": {"backend": "optiland", "version": "0.5", "error": None, "rows": rows},
            "validity": {"numeric": {"applicable": True, "warnings": {"count": 2, "first": "RuntimeWarning: x"}},
                         "unknown": [{"id": "err", "reason": "check_error", "detail": "boom"}], "materials": materials},
            "budget": BUDGET}


def test_four_sections_appear_in_both_languages():
    d = build([("Standard", math.inf, 0.0, AIR, [1.0, 1.0]), ("Standard", 10.0, 0.0, BK7, [1.5222, 1.5195]),
               ("Standard", math.inf, 0.0, AIR, [1.0, 1.0])], telecentric=True)
    shown = _shown_fixture()
    meta = {"job": "j", "lens": "a.zmx", "time": "t"}
    zh = model_report.render(d, ALL, meta, "zh", [], shown)
    for text in ("选型依据", "需求逐项", "计算有效性（系统核对）", "下一阶段预算",
                 "当前结构匹配候选：a", "../../scans/001/index.html", "分组缩短总长", "预算内无效", "两片穿插", "未评估：材料",

                 "满足", "未满足", "未知", "agent 假设", "boom", "经验值", "N-BK7", "没有存证据文件", "2 次",
                 "最多 900 秒", "对选定结构做 Hammer", "本节由系统根据记录和测量核对", "用户确认不担保这些内容"):
        assert text in zh, text
    en = model_report.render(d, ALL, meta, "en", [], shown)
    for text in ("Structure choice", "Requirements, item by item", "Computation validity (checked by the system)", "Budget for the next stage",
                 "Current structure matches: a", "invalid within budget", "not evaluated: material",
                 "met", "not met", "agent assumption", "no evidence files were kept",
                 "at most 900 s", "Hammer", "Confirming does not vouch for it", "Source found", "Data complete", "Backend supports",
                 "Actually loaded"):
        assert text in en, text
    # 没有数据时四节仍然出现，只是处于空状态 / without the data the four sections still appear, in their empty state
    bare = model_report.render(d, ALL, meta, "zh")
    assert "选型依据" in bare and "需求逐项" in bare and "计算有效性（系统核对）" in bare and "下一阶段预算" in bare


def test_material_levels_say_unknown_when_the_code_cannot_tell(saos_home):
    """四级各自的判定：没有记录、记录读不出、没有色散、没有后端信息时写“未知”，不推测。
    Each level: without a record, an unreadable record, no dispersion or no backend information it reads unknown; nothing is guessed."""
    job = jobs.create_job("mat-levels")
    job.root.joinpath("sources").mkdir(parents=True, exist_ok=True)
    d = build([("Standard", 10.0, 0.0, BK7, [1.5222, 1.5195]), ("Standard", math.inf, 0.0, MODEL, [1.608, 1.604])], telecentric=True)
    rows = {r["name"]: r for r in model_report.material_rows(job, d, None)}
    bk7 = rows["N-BK7"]["levels"]
    assert bk7["found"] == {"state": "unknown", "note": {"code": "no_record"}}
    assert bk7["complete"]["state"] == "unknown" and bk7["backend"] == {"state": "unknown", "note": {"code": "backend_unknown"}}
    assert bk7["loaded"]["state"] == "yes"
    model = next(r for r in rows.values() if r["kind"] == "model")["levels"]
    assert {model[k]["state"] for k in ("found", "complete", "backend")} == {"na"}
    # 记录读不出：资料找到未知，数据完整也未知 / an unreadable record: source found and data complete are unknown
    (job.root / materials_record.FILE).write_text("schema: [", encoding="utf-8")
    bk7 = next(r for r in model_report.material_rows(job, d, "zos") if r["name"] == "N-BK7")["levels"]
    assert bk7["found"]["state"] == "unknown" and bk7["found"]["note"]["code"] == "record_unreadable"
    assert bk7["complete"]["state"] == "unknown"
    # 有记录，色散的有效范围覆盖工作波长 / the record's valid range covers the working wavelengths
    record = {"schema": 1, "sources": {"tds": {"url": "https://example.org/tds.pdf"}},
              "materials": [{"id": "prism", "zemax": {"catalog": "SCHOTT", "name": "N-BK7"}, "source_ids": ["tds"],
                             "dispersion": {"formula": "cauchy_nm", "coefficients": [1.5, 4000, 0], "valid_range_nm": [400, 700]}}]}
    (job.root / materials_record.FILE).write_text(yaml.safe_dump(record), encoding="utf-8")
    covered = next(r for r in model_report.material_rows(job, d, "zos") if r["name"] == "N-BK7")["levels"]
    assert covered["found"]["state"] == "yes" and covered["found"]["note"]["source"] == {"kind": "sources", "ids": ["tds"]}
    assert covered["complete"]["state"] == "yes" and covered["backend"]["state"] == "yes"
    # 工作波长 800 nm 超出有效范围 / a working wavelength of 800 nm is outside the valid range
    wide = copy.deepcopy(d)
    wide["wavelengths"][0]["um"] = 0.8
    gap = next(r for r in model_report.material_rows(job, wide, "zos") if r["name"] == "N-BK7")["levels"]["complete"]
    assert gap["state"] == "no" and gap["note"]["missing"] == [800.0]
    # OpticStudio 的在用目录里没有这个牌号 / the glass is not in the catalogues OpticStudio uses
    no_cat = copy.deepcopy(d)
    no_cat["surfaces"][0]["material_source"] = {"kind": "catalog", "name": "XYZ", "catalogs": []}
    assert next(r for r in model_report.material_rows(job, no_cat, "zos") if r["name"] == "XYZ")["levels"]["backend"]["state"] == "no"
    # 没有实测折射率：实际加载未知 / no measured index: actually loaded is unknown
    no_n = copy.deepcopy(d)
    no_n["surfaces"][0]["n"] = None
    assert next(r for r in model_report.material_rows(job, no_n, "zos") if r["name"] == "N-BK7")["levels"]["loaded"]["state"] == "unknown"


def test_requirement_status_has_met_unmet_and_unknown():
    """需求逐项的三种状态：达标、未达标、没有有效值或判定出错（未知）；没有判定方法的条目没有界限，记为无界限。
    The three statuses: met, not met, unknown (no valid value or a failed check); an item without a check has no bound."""
    doc = {"schema": 2,
           "constants": [{"id": "lens", "description": "样品", "value": "a.zmx", "source": "user"},
                         {"id": "cap", "description": "总长上限", "check": {"operand": {"type": "TOTR"}}, "goal": {"max": 150.0}, "source": "user"}],
           "variables": [{"id": "radius", "description": "半径", "range": {"min": 5.0, "max": 50.0}, "check": {"operand": {"type": "RAD"}},
                          "source": "agent assumption: 常见范围"}],
           "objectives": [{"id": "spot", "description": "点列", "check": {"operand": {"type": "SPOT"}}, "origin": "user",
                           "must": {"max": 5.0}, "scale": 1.0, "source": "user"},
                          {"id": "cost", "description": "成本", "check": {"operand": {"type": "COST"}}, "origin": "agent", "minimize": True,
                           "scale": 1.0, "source": "agent assumption: 经验"}],
           "summary": "x"}
    measured = {"backend": "optiland", "version": "0.5", "error": None,
                "values": {"cap": 100.0, "radius": 20.0, "spot": 7.0, "cost": 3.0}, "errors": {"radius": "bad read"}, "invalid": []}
    rows = {r["id"]: r for r in model_report.requirement_rows(doc, measured)}
    assert rows["cap"]["status"] == "met" and rows["cap"]["bound"] == {"max": 150.0}
    assert rows["spot"]["status"] == "unmet" and rows["spot"]["value"] == 7.0 and rows["spot"]["assumed"] is False
    assert rows["radius"]["status"] == "unknown" and rows["radius"]["reason"] == "check_error" and rows["radius"]["detail"] == "bad read"
    assert rows["radius"]["assumed"] is True
    assert rows["lens"]["status"] == "no_bound" and rows["lens"]["reason"] == "no_check" and rows["lens"]["bound"] == {"fixed": "a.zmx"}
    assert rows["cost"]["status"] == "pursuit" and rows["cost"]["bound"] == {"direction": "min", "ideal": 0.0}
    # 测量没有完成：全部未知 / measurement did not run: every checked item is unknown
    none = {r["id"]: r for r in model_report.requirement_rows(doc, None)}
    assert none["spot"]["status"] == "unknown" and none["spot"]["reason"] == "not_measured"
    # 没有有效值：未知 / no valid value: unknown
    dead = {r["id"]: r for r in model_report.requirement_rows(doc, {**measured, "values": {**measured["values"], "spot": None}, "errors": {}})}
    assert dead["spot"]["status"] == "unknown" and dead["spot"]["reason"] == "no_valid_value"


def _describe(daemon, job, notes=None, lang="zh"):
    res = daemon.handle({"op": "lens.describe", "args": {"job": job.id, "lang": lang, "notes": notes}})
    assert res["ok"], res
    return res["result"]


def _started_daemon(fake_lens_file, name):
    backend = FakeBackend()
    daemon = Daemon(backend, backend.start(), "extension")
    job = jobs.create_job(name)
    for op, args in (("lens.import", {"src": str(fake_lens_file), "name": "start.zmx"}), ("lens.load", {"file": "start.zmx"})):
        assert daemon.handle({"op": op, "args": {"job": job.id, **args}})["ok"]
    return backend, daemon, job


def test_describe_measures_requirements_and_records_digest_and_unknowns(saos_home, fake_lens_file):
    backend, daemon, job = _started_daemon(fake_lens_file, "sections")
    _write_demand(job)
    notes = {"surfaces": {"0": "object"}, "first_order": ["f"], "exploration": "prescribed", "next_budget": BUDGET}
    out = _describe(daemon, job, notes)
    model = json.loads((job.root / out["folder"] / "model.json").read_text(encoding="utf-8"))
    length = next(r for r in model["shown"]["requirements"]["rows"] if r["id"] == "length")
    assert length["status"] == "met" and length["value"] is not None and model["shown"]["requirements"]["backend"] == "fake"
    assert model["shown"]["selection"] == {"mode": "free", "chosen": None, "rows": []}
    assert any("No screening record" in w for w in out["warnings"])
    assert model["shown"]["budget"] == BUDGET and not any("next_budget" in w for w in out["warnings"])
    assert model["shown_sha"] == model_gate.digest(model["shown"]) and out["shown_sha"] == model["shown_sha"]
    # 需求都测了，未知只来自材料（假后端的后端不明、没有材料记录）/ every requirement was measured; the unknowns come from the materials only (fake backend, no record)
    assert not any(u["section"] == "requirements" for u in model["unknowns"])
    assert {u["level"] for u in model["unknowns"] if u["section"] == "materials"} == {"found", "complete", "backend"}
    text = (job.root / out["folder"] / "index.html").read_text(encoding="utf-8")
    assert "需求逐项" in text and "样品单片镜" in text
    # 摘要与页面语言无关：英文页面的 shown_sha 相同 / the digest does not depend on the page language
    assert _describe(daemon, job, notes, lang="en")["shown_sha"] == out["shown_sha"]


def test_approval_records_the_digest_and_the_unknowns_shown(saos_home, fake_lens_file):
    backend, daemon, job = _started_daemon(fake_lens_file, "approve-sections")
    out = _describe(daemon, job, {"surfaces": {"0": "object"}, "first_order": ["f"], "exploration": "prescribed", "next_budget": BUDGET})
    model = json.loads((job.root / out["folder"] / "model.json").read_text(encoding="utf-8"))
    record = model_gate.approve(job, out["folder"], "模型可以")
    assert record["shown_sha"] == model["shown_sha"] and record["unknowns_acknowledged"] == model["unknowns"]
    assert record["unknowns_acknowledged"], "no demand: the requirements are unknown and must be recorded"
    saved = json.loads((job.root / out["folder"] / model_gate.APPROVAL_FILE).read_text(encoding="utf-8"))
    assert saved["shown_sha"] == model["shown_sha"]
    # 旧格式：没有四节的 model.json 照样能确认，确认后的结构照样被接受 / an older model.json without the four sections is approved and accepted
    old = {k: v for k, v in model.items() if k not in ("shown", "shown_sha", "unknowns", "approval_schema", "evidence_context")}
    (job.root / out["folder"] / "model.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    (job.root / out["folder"] / model_gate.APPROVAL_FILE).unlink()
    job.seal(job.root / out["folder"] / "model.json")
    old_record = model_gate.approve(job, out["folder"], "旧格式可以")
    assert "shown_sha" not in old_record and "unknowns_acknowledged" not in old_record
    assert model_gate.require_approved(job, backend.describe_model(None))["model"] == out["folder"]


def test_next_budget_format_is_checked_and_missing_is_a_warning(saos_home):
    job = jobs.create_job("budget")
    d = build([("Standard", math.inf, 0.0, AIR, [1.0, 1.0]), ("Standard", math.inf, 0.0, AIR, [1.0, 1.0])], telecentric=True)
    assert model_report.next_budget(None) is None and model_report.next_budget({"surfaces": {}}) is None
    assert model_report.next_budget({"next_budget": BUDGET}) == {"max_seconds": 900.0, "plan": BUDGET["plan"]}
    bad = [{"max_seconds": 0, "plan": "x"}, {"max_seconds": -1, "plan": "x"}, {"max_seconds": True, "plan": "x"},
           {"max_seconds": "900", "plan": "x"}, {"max_seconds": 900}, {"max_seconds": 900, "plan": " "},
           {"max_seconds": 900, "plan": "x", "extra": 1}, "900 s", {"max_seconds": float("inf"), "plan": "x"}]
    for value in bad:
        with pytest.raises(SaosError) as refused:
            model_report.write(job, d, job.root / "models" / "001-x", {"next_budget": value}, "x.zmx", "en")
        assert refused.value.code == ErrorCode.BAD_ARGS, value
    folder = job.root / "models" / "002-x"
    folder.mkdir(parents=True)
    out = model_report.write(job, d, folder, {"surfaces": {"0": "a", "1": "b", "2": "c", "3": "d"}, "first_order": ["f"]}, "x.zmx", "en")
    assert any("next_budget" in w for w in out["warnings"])
    assert json.loads((folder / "model.json").read_text(encoding="utf-8"))["shown"]["budget"] is None




def test_prescribed_structure_shows_the_users_reason_and_needs_no_selection(saos_home):
    job = jobs.create_job("prescribed")
    _write_demand(job, {**DEMAND_DOC, "structure": {"mode": "prescribed", "reason": "样品孔径固定，用户已定光路"}})
    d = build([("Standard", math.inf, 0.0, AIR, [1.0, 1.0]), ("Standard", 10.0, 0.0, BK7, [1.5222, 1.5195]),
               ("Standard", math.inf, 0.0, AIR, [1.0, 1.0])], telecentric=True)
    folder = job.root / "models" / "001-x"
    folder.mkdir(parents=True)
    out = model_report.write(job, d, folder, {**ALL, "next_budget": BUDGET}, "x.zmx", "zh")
    model = json.loads((folder / "model.json").read_text(encoding="utf-8"))
    assert model["shown"]["selection"] == {"mode": "prescribed", "reason": "样品孔径固定，用户已定光路"}
    # 结构由用户指定：不要求候选比较，也不出现选型的警告（no_exploration 另有规则，与此无关）
    # fixed by the user: no candidate comparison is asked for; the selection warnings do not appear (no_exploration is a separate rule)
    assert not any("not fixed by the user" in w or "saos scan select" in w for w in out["warnings"])
    assert "样品孔径固定，用户已定光路" in (folder / "index.html").read_text(encoding="utf-8")


def test_screening_matches_by_fingerprint_and_stale_checks_only_warn(saos_home):
    job = jobs.create_job("screening-choice")
    _write_demand(job)
    d = build([("Standard", math.inf, 0, AIR, [1, 1]), ("Standard", 10, 0, BK7, [1.5222, 1.5195]),
               ("Standard", math.inf, 0, AIR, [1, 1])], telecentric=True)
    fp = model_gate.digest(model_gate.fingerprint(d))
    scan = job.root / "scans" / "001"
    scan.mkdir(parents=True)
    (scan / "plan.json").write_text(json.dumps({"mode": "candidates", "demand_sha": demand.compute_digest(demand.load_job(job.root)),
                                               "method_sha": "older-checks"}), encoding="utf-8")
    rows = [{"id": "a", "verdict": "valid", "form": "Cooke", "provenance": {"patent": "测试来源"},
             "structure": {"end": {"fingerprint": fp}}, "prediction": {"first_order": {"efl": 50}},
             "start": {"shortfall": 2}, "shortfall": 1},
            {"id": "b", "verdict": "invalid", "problems": ["预算内无效"]}]
    (scan / "result.json").write_text(json.dumps({"mode": "candidates", "rows": rows}), encoding="utf-8")
    job.seal(scan / "plan.json", scan / "result.json")
    folder = job.root / "models" / "001-x"
    folder.mkdir(parents=True)
    out = model_report.write(job, d, folder, {**ALL, "next_budget": BUDGET}, "x.zmx", "zh")
    saved = json.loads((folder / "model.json").read_text(encoding="utf-8"))
    assert saved["shown"]["selection"]["matches"] == ["a"]
    assert any("Check methods changed" in w for w in out["warnings"])
    page = (folder / "index.html").read_text(encoding="utf-8")
    assert "当前结构匹配候选：a" in page and "EFL" in page and "预算内无效" in page
    assert not (scan / "selection.json").exists()
