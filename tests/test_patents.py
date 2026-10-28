"""专利镜头库：CODE V .seq 的受控解析与转换、近轴预筛、索引检索，以及作为 starts.generate 的候选。只用合成的小文件。"""
from __future__ import annotations

import copy
import json
import math

import pytest

from saos.protocol import SaosError
from saos.starts import patents, seq

HEAD = """RDM;LEN     "VERSION:          LENS VERSION: 42"
TITLE 'USA   PATENT 1234567  TESTER'
EPD   10.0
DIM   M
WL    656.3 587.6 486.1
REF   2
WTW   1 1 1
INI   'ORA'
XAN   0.0 0.0 0.0
YAN   0.0 7.0 10.0
VUY   0.0 0.1 0.2
VLY   0.0 0.1 0.2
SO    0.0 0.1e11
"""
SINGLET = HEAD + """S     50.0 5.0 516800.641700
  STO
S     -50.0 45.0
  PIM
SI    0.0 -0.05
GO
"""
ASPHERE_AND_MIRROR = HEAD + """S     0.0 1.0
  STO
S     -200.0 -50.0 REFL
S     30.0 -2.0 CAFL_SPECIAL
  ASP
  K   -1.0
  IC  Yes; CUF 0.0
  A   0.1e-4; B -0.2e-7; C 0.0; D 0.0
S     60.0 -20.0 'K8'
  PIM
SI    0.0 0.0
GO
"""
ZOOM = HEAD + """S     50.0 5.0 516800.641700
  STO
S     -50.0 10.0
S     -60.0 2.0 620040.363000
S     200.0 40.0
  PIM
SI    0.0 0.0
ZOO   2
ZOO   PIM YES YES
ZOO   EPD 10.0 8.0
ZOO   THI S2 10.0 20.0
ZOO THC S2 100 100
GO
"""


def thick_lens(n, r1, r2, d):
    power = (n - 1) * (1 / r1 - 1 / r2) + (n - 1) ** 2 * d / (n * r1 * r2)
    return 1 / power, (1 - (n - 1) * d / (n * r1)) / power


def test_singlet_converts_with_model_glass_pim_solve_and_defocus():
    data = seq.parse(SINGLET, "or00001")
    out = seq.to_prescription(data)
    p = out["prescription"]
    assert p["wavelengths"]["um"] == pytest.approx([.6563, .5876, .4861]) and p["wavelengths"]["primary"] == 2
    assert p["object"]["thickness"] == math.inf and p["aperture"] == {"type": "entrance_pupil_diameter", "value": 10.0}
    first, second, defocus = p["surfaces"]
    assert first["material"] == {"model": {"nd": 1.5168, "vd": 64.17, "dpgf": 0.0}} and first["stop"]
    assert second["solves"]["thickness"] == {"type": "marginal_ray_height", "height": 0.0, "zone": 0.0}
    assert defocus["thickness"] == -0.05 and defocus["material"] == ""
    assert any("渐晕" in item for item in out["not_converted"])
    efl, bfl = thick_lens(1.5168, 50.0, -50.0, 5.0)
    px = seq.paraxial(data)
    assert px["efl"] == pytest.approx(efl, rel=1e-12) and px["bfl"] == pytest.approx(bfl, rel=1e-12)
    assert px["f_number"] == pytest.approx(efl / 10.0) and px["half_field_deg"] == 10.0
    assert seq.elements(data) == {"elements": 1, "groups": 1, "mirrors": 0, "signs": "+"}


def test_materials_are_never_substituted():
    data = seq.parse(ASPHERE_AND_MIRROR, "or00002")
    with pytest.raises(SaosError) as err:
        seq.to_prescription(data)
    assert "CAFL_SPECIAL" in err.value.message and "K8" in err.value.message
    p = seq.to_prescription(data, materials={"CAFL_SPECIAL": "CAF2", "K8": {"model": {"nd": 1.5168, "vd": 64.2}}})["prescription"]
    mirror, lens, back = p["surfaces"][1:4]
    assert mirror["material"] == "MIRROR" and mirror["thickness"] == -50.0
    assert lens["type"] == "EvenAspheric" and lens["conic"] == -1.0 and lens["material"] == "CAF2"
    assert lens["params"] == {"par2": 1e-5, "par3": -2e-8}
    assert back["material"] == {"model": {"nd": 1.5168, "vd": 64.2, "dpgf": 0.0}}
    with pytest.raises(SaosError, match="nd"):
        seq.paraxial(data)


def test_unknown_commands_and_structures_are_refused():
    data = seq.parse(SINGLET.replace("GO\n", "POS   YES NO NO\nGO\n"), "or00003")
    assert data["unrecognized"] == ["POS YES NO NO"]
    with pytest.raises(SaosError, match="POS"):
        seq.to_prescription(data)
    assert seq.to_prescription(data, allow_unrecognized=True)["unrecognized"] == ["POS YES NO NO"]
    curved = seq.parse(SINGLET.replace("SO    0.0 0.1e11", "SO    500.0 300.0"), "or00004")
    with pytest.raises(SaosError, match="物面"):
        seq.to_prescription(curved)
    no_stop = seq.parse(SINGLET.replace("  STO\n", ""), "or00005")
    assert "光阑面数为 0" in seq.problems(no_stop)


def test_a_zoom_position_is_a_fixed_lens_and_needs_an_explicit_position():
    data = seq.parse(ZOOM, "or00006")
    with pytest.raises(SaosError, match="position"):
        seq.to_prescription(data)
    wide, tele = (seq.to_prescription(data, position=k)["prescription"] for k in (1, 2))
    assert [s["thickness"] for s in wide["surfaces"][:2]] == [5.0, 10.0] and tele["surfaces"][1]["thickness"] == 20.0
    assert tele["aperture"]["value"] == 8.0
    assert seq.paraxial(data, 1)["efl"] != pytest.approx(seq.paraxial(data, 2)["efl"])
    assert seq.elements(data, 1)["signs"] == "+-"
    with pytest.raises(SaosError):
        seq.geometry(data, 3)


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "patents"
    (root / "seq").mkdir(parents=True)
    for name, text in (("or00001", SINGLET), ("or00002", ASPHERE_AND_MIRROR), ("or00006", ZOOM),
                       ("or00007", SINGLET.replace("EPD   10.0", "EPD   5.0").replace("YAN   0.0 7.0 10.0", "YAN   0.0 3.0 5.0"))):
        (root / "seq" / f"{name}.seq").write_text(text, encoding="utf-8")
    patents.build_index(root)
    return root


def test_index_search_ranks_the_closest_covering_example_first(library):
    index = {r["id"]: r for r in patents.load_index(library)}
    assert index["or00001"]["source"] == {"country": "US", "number": "1234567", "rest": "TESTER"}
    assert index["or00002"]["materials_to_map"] == ["CAFL_SPECIAL", "K8"] and index["or00002"]["paraxial"] is None
    assert "zoom" in index["or00006"]["tags"] and index["or00006"]["zoom_ratio"] > 1
    found = patents.search(half_field_deg=4, f_number=10, wavelengths_um=[.5876], root=library)
    assert [c["id"] for c in found["candidates"]] == ["or00007", "or00001"]
    assert patents.search(half_field_deg=8, f_number=10, root=library)["candidates"][0]["id"] == "or00001"
    assert patents.search(half_field_deg=20, f_number=10, root=library)["candidates"] == []
    assert [c["id"] for c in patents.search(half_field_deg=20, f_number=10, allow_extrapolation=True, root=library)["candidates"]] == ["or00001", "or00007"]
    zoom = patents.search(f_number=12, zoom=True, root=library)["candidates"]
    assert zoom[0]["id"].startswith("or00006-p") and zoom[0]["position"] in (1, 2)


def test_prescription_refuses_a_file_that_changed_after_indexing(library):
    assert patents.prescription("or00006-p2", root=library)["provenance"]["position"] == 2
    path = library / "seq" / "or00001.seq"
    path.write_text(SINGLET.replace("45.0", "44.0"), encoding="utf-8")
    with pytest.raises(SaosError, match="索引"):
        patents.prescription("or00001", root=library)
    with pytest.raises(SaosError):
        patents.prescription("cooke", root=library)


def test_generate_takes_patent_candidates(saos_home, library, monkeypatch):
    import yaml
    from saos import demand, jobs, starts
    from saos.starts import prediction
    job = jobs.create_job("starts-patents")
    doc = {"schema": 2, "constants": [
        {"id": "f", "description": "焦距", "value": 50, "unit": "mm", "source": "自动化测试夹具"},
        {"id": "h", "description": "半视场", "value": 5, "unit": "deg", "source": "自动化测试夹具"},
        {"id": "n", "description": "f数", "value": 8, "source": "自动化测试夹具"},
        {"id": "w", "description": "波长", "value": [.4861327, .5875618, .6562725], "unit": "um", "source": "自动化测试夹具"}],
        "variables": [], "objectives": [{"id": "length", "description": "总长", "check": {"operand": {"type": "TOTR"}}, "origin": "user",
                                         "must": {"max": 200}, "source": "自动化测试夹具"}], "summary": demand.QUESTION_LINE["zh"]}
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    (job.root / "prepare.py").write_text("def run(w):\n    return None\n", encoding="utf-8")
    built = []

    class Backend:
        def start(self): pass
        def close(self): pass
        def build(self, data): built.append(copy.deepcopy(data))
        def operand(self, *args): return 25.0

    monkeypatch.setattr(prediction, "predict", lambda backend, **kwargs: {"first_order": {"efl": 50}})
    out = starts.generate(job, {"efl_mm": "f", "half_field_deg": "h", "f_number": "n", "wavelengths_um": "w"},
                          short_seconds=1, prepare="prepare.py", backend_factory=Backend, patents_root=library,
                          patents=["or00001", "or00006", {"id": "or00002", "materials": {"CAFL_SPECIAL": "CAF2"}}])
    assert out["count"] == 1 and len(built) == 2
    assert built[1]["surfaces"][0]["radius"] == pytest.approx(100.0)
    reasons = {e["id"]: e["reasons"] for e in out["excluded"]}
    assert "or00006" in reasons and "or00002" in reasons
    plan = json.loads((job.root / out["screen_plan"]).read_text(encoding="utf-8"))
    provenance = plan["candidates"][0]["provenance"]
    assert provenance["file"] == "seq/or00001.seq" and provenance["seq_sha"] == provenance["sha256"] and provenance["validation"] == "patent_unverified"
