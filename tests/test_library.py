"""共享参考库的统一入口：按文本和字段取条目并带出来源、拒绝未知字段与外推，仓库里的每个库都读得出来。"""
from __future__ import annotations

import json

import pytest

from saos import library
from saos.protocol import ErrorCode, SaosError


@pytest.fixture
def tiny(tmp_path, monkeypatch):
    (tmp_path / "library" / "mechanics").mkdir(parents=True)
    (tmp_path / "library" / "mechanics" / "materials.json").write_text(json.dumps({
        "materials": [{"id": "a", "name": "Alpha Steel", "category": "barrel", "source": "s1"},
                      {"id": "b", "name": "Beta Glass", "category": "mirror_substrate", "source": "s2"},
                      {"id": "c", "name": "Gamma Glass", "category": "mirror_substrate", "source": "missing"}],
        "sources": {"s1": {"title": "one"}, "s2": {"title": "two"}}}), encoding="utf-8")
    (tmp_path / "library" / "materials").mkdir()
    (tmp_path / "library" / "materials" / "catalog.json").write_text(json.dumps({
        "materials": [{"id": "r", "dispersion": {"formula": "cauchy_nm", "coefficients": [1.5, 4000.0, 0.0], "valid_range_nm": [400, 800]}},
                      {"id": "bare", "dispersion": None}],
        "sources": {}}), encoding="utf-8")
    monkeypatch.setattr(library.config, "REPO_ROOT", tmp_path)


def test_find_matches_text_anywhere_and_fields_exactly_and_brings_the_cited_sources(tiny):
    glass = library.find("mechanics", "glass")
    assert glass["total"] == 2 and [i["id"] for i in glass["items"]] == ["b", "c"]
    assert glass["items"][0]["cited"] == {"s2": {"title": "two"}}
    # 引用了库里没有的来源：不编造，cited 为空 / a source the library lacks is not invented
    assert glass["items"][1]["cited"] == {}
    both = library.find("mechanics", "GLASS", category="mirror_substrate", limit=1)
    assert both["total"] == 2 and len(both["items"]) == 1
    assert library.find("mechanics", category="barrel")["items"][0]["id"] == "a"


def test_unknown_libraries_fields_and_limits_are_bad_args(tiny):
    for call in (lambda: library.find("nope"), lambda: library.find("mechanics", colour="red"),
                 lambda: library.find("mechanics", limit=0), lambda: library.index("missing", 500)):
        with pytest.raises(SaosError) as exc:
            call()
        assert exc.value.code == ErrorCode.BAD_ARGS


def test_index_follows_the_dispersion_model_and_refuses_to_extrapolate(tiny):
    assert library.index("r", 500) == 1.5 + 4000.0 / 500**2
    for material, wave in (("r", 399.9), ("r", 801), ("r", float("nan")), ("bare", 500)):
        with pytest.raises(SaosError) as exc:
            library.index(material, wave)
        assert exc.value.code == ErrorCode.BAD_ARGS


def test_a_missing_library_file_is_reported_not_raised_by_the_catalog(tiny):
    rows = {c["library"]: c for c in library.catalog()}
    assert rows["mechanics"]["entries"] == 3 and "category" in rows["mechanics"]["fields"]
    assert rows["coatings"]["entries"] is None and "coatings" in rows["coatings"]["error"]
    with pytest.raises(SaosError) as exc:
        library.find("coatings")
    assert exc.value.code == ErrorCode.FILE_NOT_FOUND


def test_every_library_in_the_repository_loads():
    # 库文件改了结构而入口没跟上时在这里暴露 / catches a library whose layout changed under the entry point
    broken = {c["library"]: c.get("error") for c in library.catalog() if not c["entries"]}
    assert broken == {}
