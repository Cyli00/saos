"""材料层回归：ZOS-API 实测观测、历史应用级 INDX，以及解析恒等式。"""

import csv
import json
from pathlib import Path

import numpy as np
import pytest

from rtc import zmx
from rtc.material import Air, Catalog, CatalogGlass, Environment, Glass, GlassCatalog, MaterialInvalid, MaterialUnsupported, Model, resolve
from rtc.material.agf import parse_agf
from rtc.material.air import n_air
from rtc.material.formulas import dispersion
from rtc.material.model import model_index

ROOT = Path(__file__).resolve().parent
VECTORS = json.loads((ROOT / "fixtures/material/native-vectors.json").read_text(encoding="utf-8"))


def test_model_matches_252_native_observations_with_vectorized_wavelengths():
    for w, nd, vd, dpgf, actual in VECTORS["model"]:
        assert model_index([w, w], nd, vd, dpgf) == pytest.approx([actual, actual], rel=0, abs=1e-14)


def test_air_matches_60_native_observations():
    for w, t, p, actual in VECTORS["air"]:
        assert n_air(w, t, p) == pytest.approx(actual, rel=0, abs=1e-14)


def test_all_13_formulas_match_832_native_environment_observations():
    coefficients = {1: [2.27, .01, .012, .0002, .00001, .000001],
                    2: [1.03961212, .00600069867, .231792344, .0200179144, 1.01046945, 103.560653]}
    common = [2.27, .01, .012, .0002, .00001, .000001, 1e-8, 1e-10, 1e-12, 1e-14, 1e-16, 1e-18]
    for kind, w, tref, t, p, adjust, tk, actual in VECTORS["catalog"]:
        glass = Glass("SYNTHETIC", kind, 0.0, 0.0, coef=coefficients.get(kind, common), tref=tref,
                      thermal=(1e-6, 2e-8, -1e-10, 3e-7, 4e-9, tk), wavelength_range=(0.1, 3.0))
        assert CatalogGlass(glass).index([w, w], Environment(t, p, adjust)) == pytest.approx([actual, actual], rel=0, abs=1e-14)


def test_792_real_catalog_native_observations():
    for kind, name, w, t, p, adjust, actual in VECTORS["real"]:
        r = VECTORS["records"][str(kind)]
        glass = Glass(name, kind, r["nd"], 0.0, coef=r["CD"], tref=r["TD"][6],
                      thermal=tuple(r["TD"][:6]), wavelength_range=tuple(r["LD"]))
        assert CatalogGlass(glass).index(w, Environment(t, p, adjust)) == pytest.approx(actual, rel=0, abs=1e-14)


def test_model_environment_matches_108_native_observations():
    for w, dpgf, t, p, adjust, actual in VECTORS["model_environment"]:
        medium = resolve(Model(1.5168, 64.17, dpgf))
        assert medium.index(w, Environment(t, p, adjust)) == pytest.approx(actual, rel=0, abs=1e-14)


@pytest.mark.parametrize("jobs,output", [("jobs.txt", "op.csv"), ("jobs2.txt", "r2/op.csv"), ("jobs3.txt", "r3/op.csv")])
def test_original_model_glass_indx_oracles(jobs, output):
    folder = ROOT / "fixtures/golden/modelglass"
    with (folder / output).open(encoding="utf-8-sig", newline="") as source:
        rows = iter(csv.DictReader(source))
        for line in (folder / jobs).read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if not fields:
                continue
            if fields[0] == "MODEL":
                nd, vd = map(float, fields[2:4])
            elif fields[0] == "WAVE":
                w = float(fields[1])
            elif fields[:2] == ["OP", "INDX"]:
                row = next(rows)
                assert row["op"] == "INDX"
                assert model_index(w, nd, vd) == pytest.approx(float(row["value"]), rel=0, abs=1e-8)
        assert next(rows, None) is None


@pytest.mark.parametrize("environment", [Environment(-40, 0, True), Environment(60, .5, True), Environment(25, 1, False)])
def test_air_in_a_single_environment_is_exactly_one(environment):
    assert np.array_equal(Air().index([.36, .55, 2.5], environment), np.ones(3))


def test_agf_preserves_all_fields_and_does_not_limit_cd_search_to_seven_lines(tmp_path):
    text = """CC catalog comment
NM TEST 11 516640 1.5 60 1 2 3
GC glass comment
ED 7.1 8.2 2.5 0.01 1 0
MD 1 _ 3 _ 5
OD ? 2 3 4 5 6
IT .8 .9 10
BD .6 1 2 3
LD .3 3
TD 1e-6 2e-8 -1e-10 3e-7 4e-9 -.17 25
CD 1 .01 .2 .02 .1 .03 .05 .04 .01 .05
IT .4 .8 10
"""
    document = parse_agf(text, catalog="EXAMPLE")
    g = document.glasses["TEST"]
    assert len(g.coef) == 10 and g.coef[-1] == .05
    assert g.catalog_nd == 1.5 and g.catalog_vd == 60
    assert g.thermal == (1e-6, 2e-8, -1e-10, 3e-7, 4e-9, -.17) and g.tref == 25
    assert g.ed == (7.1, 8.2, 2.5, .01) and g.ignore_expansion
    assert g.md == (1, None, 3, None, 5) and g.od == (-1, 2, 3, 4, 5, 6)
    assert g.status == 2 and g.melt_frequency == 3 and g.exclude == 1
    assert [row[0] for row in g.it] == pytest.approx([.4, .8], abs=1e-7)
    assert len(g.bd) == 1 and g.dpgf == .01
    (tmp_path / "A.AGF").write_text(text, encoding="utf-16")
    (tmp_path / "B.AGF").write_text(text.replace(".17 25", ".17 20"), encoding="utf-8")
    directory = GlassCatalog(tmp_path)
    assert directory.get("test", ["B", "A"]).tref == 20
    assert directory.get("test", ["A", "B"]).tref == 25


def test_ld_native_tolerance_and_failure_instead_of_air_fallback():
    medium = CatalogGlass(Glass("X", 1, 1.5, 0, [2.25, 0, 0, 0, 0, 0], wavelength_range=(.4, 1.0)))
    assert medium.index(.4 * .999, Environment()) == 1.5
    assert medium.index(1.0 / .999, Environment()) == 1.5
    for w in (.399, 1.002):
        with pytest.raises(MaterialUnsupported, match="LD"):
            medium.index(w, Environment())


@pytest.mark.parametrize("w", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_wavelengths_have_one_error_type(w):
    with pytest.raises(MaterialInvalid):
        resolve(Model(1.5, 50)).index(w, Environment())


def test_environment_and_model_parsing_and_legacy_fields(tmp_path):
    path = tmp_path / "model.zmx"
    path.write_text("VERS 190513\nENVD 60 .5 1\nSURF 0\nSURF 1\n  GLAS ___BLANK 1 0 1.63 55 .03\n", encoding="utf-8")
    lens = zmx.parse(path)
    assert lens.environment == Environment(60, .5, True)
    assert lens.surface(1).material == Model(1.63, 55, .03)
    lens.surface(1).material = Catalog("N-BK7")
    assert lens.surface(1).glass == "N-BK7" and lens.surface(1).model_nd is None
    lens.surface(1).material = None
    assert lens.surface(1).glass == "" and lens.surface(1).model_dpgf == 0


@pytest.mark.parametrize("version,t,p,flag", [(50914, 20, 1, True), (50915, 20, 1, False), (190513, 25, 1, True)])
def test_envd_missing_adjust_uses_recovered_version_rule(tmp_path, version, t, p, flag):
    path = tmp_path / "env.zmx"
    path.write_text(f"VERS {version}\nENVD {t} {p}\n", encoding="utf-8")
    assert zmx.parse(path).environment == Environment(t, p, flag)


def test_formula_zero_fallback_and_unregistered_formula():
    assert dispersion([.5, 1.0], [0.0] * 6, 1, 1.52) == pytest.approx([1.52, 1.52])
    with pytest.raises(MaterialUnsupported):
        dispersion(.5, [1] * 10, 14)
