"""在产玻璃目录：只留 Standard、Preferred 的牌号，每个牌号的数据行原样保留。"""

from __future__ import annotations

from saos import glasscat

AGF = "\r\n".join([
    "CC Updated Sep. 2026",
    "NM H-K9L 2 517642 1.5168 64.2 0 1 1",
    "CD 1 2 3 4 5 6 0 0 0 0",
    "TD 0 0 0 0 0 20 0",
    "NM H-OLD 2 600400 1.6 40 0 2 1",
    "CD 9 9 9 9 9 9 0 0 0 0",
    "NM H-ZF52 2 847238 1.84666 23.78 0 0 1",
    "CD 7 7 7 7 7 7 0 0 0 0",
    "NM H-RARE 2 700300 1.7 30 0 3 1",
    "",
])


def test_in_production_keeps_standard_and_preferred_glasses_verbatim(tmp_path):
    text, counts = glasscat.in_production(AGF, "TEST_INPROD: made for a test")
    assert counts == {"Preferred": 1, "Obsolete": 1, "Standard": 1, "Special": 1}
    lines = text.split("\r\n")
    assert lines[0] == "CC TEST_INPROD: made for a test" and "H-OLD" not in text and "H-RARE" not in text
    assert lines[1:4] == ["NM H-K9L 2 517642 1.5168 64.2 0 1 1", "CD 1 2 3 4 5 6 0 0 0 0", "TD 0 0 0 0 0 20 0"]
    path = tmp_path / "TEST_INPROD.AGF"
    path.write_bytes(text.encode("utf-16"))
    glasses = glasscat.load_catalog(path)
    assert [(g.name, g.status) for g in glasses] == [("H-K9L", "Preferred"), ("H-ZF52", "Standard")]


def _zos_backend(glass_dir, monkeypatch, in_use=("SCHOTT",), broken=()):
    """不连 OpticStudio 的 ZosBackend：在用目录是个替身，玻璃目录文件夹指向 glass_dir。"""
    from types import SimpleNamespace

    from saos.backend.zos import ZosBackend

    class Catalogs:
        def __init__(self):
            self.used = set(in_use)

        def IsCatalogInUse(self, name):  # noqa: N802 - ZOS-API 的名字
            return name in self.used

        def AddCatalog(self, name):  # noqa: N802
            if name not in broken:
                self.used.add(name)

        def GetCatalogsInUse(self):  # noqa: N802
            return sorted(self.used)

        def GetMaterialsInCatalog(self, name):  # noqa: N802
            return {"SCHOTT": ["SK16", "N-BK7"], "SAOS_CDGM_INPROD": ["H-K9L", "H-ZF52"]}.get(name, [])

    backend = ZosBackend.__new__(ZosBackend)
    backend._material_cache, backend._glass_status = {}, {}
    backend.app = SimpleNamespace(PrimarySystem=SimpleNamespace(SystemData=SimpleNamespace(MaterialCatalogs=Catalogs())))
    monkeypatch.setattr(ZosBackend, "_glass_dir", lambda self: glass_dir)
    return backend


def test_substitution_catalog_defaults_to_in_production_and_is_added_only_when_used(tmp_path, monkeypatch):
    import pytest

    from saos.protocol import SaosError

    backend = _zos_backend(tmp_path, monkeypatch, broken={"BROKEN"})
    assert backend.substitute_catalog(None) == ""  # 没装在产目录：全部在用目录 / not installed: every catalog in use
    (tmp_path / "SAOS_CDGM_INPROD.AGF").write_bytes(glasscat.in_production(AGF, "test")[0].encode("utf-16"))
    assert backend.substitute_catalog(None) == "SAOS_CDGM_INPROD" and backend.substitute_catalog("schott") == "SCHOTT"
    used = backend.system.SystemData.MaterialCatalogs.used
    assert used == {"SCHOTT"}  # 解析目录名不改镜头 / resolving the name leaves the lens alone
    assert backend.catalog_has("SK16", "SAOS_CDGM_INPROD") is False
    assert backend.catalog_has("SK16", "NOPE") is None and backend.catalog_has("SK16", "") is None
    assert backend.catalog_has("1.62,60.3", "SAOS_CDGM_INPROD") is None  # 模型玻璃不算 / a model glass is no catalog glass
    backend._use_catalog("SAOS_CDGM_INPROD")
    assert backend.catalog_has("H-K9L", "SAOS_CDGM_INPROD") is True
    backend._use_catalog("")
    assert used == {"SCHOTT", "SAOS_CDGM_INPROD"}
    with pytest.raises(SaosError, match="cannot load glass catalog BROKEN"):
        backend._use_catalog("BROKEN")


def test_inprod_refuses_a_maker_file_that_does_not_match_the_manifest(tmp_path, monkeypatch, capsys):
    import hashlib
    import importlib.util
    import json

    from saos import config

    spec = importlib.util.spec_from_file_location("inprod", config.REPO_ROOT / "library" / "glasscat" / "inprod.py")
    inprod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inprod)
    raw = AGF.encode("utf-16")
    (tmp_path / "MAKER.AGF").write_bytes(raw)
    entry = {"file": "MAKER.AGF", "catalog": "TEST_INPROD", "edition": "test", "sha256": hashlib.sha256(raw).hexdigest()}
    (tmp_path / "manifest.json").write_text(json.dumps({"catalogs": [entry]}), encoding="utf-8")
    monkeypatch.setattr(inprod, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["inprod.py", "--glasscat", str(tmp_path)])
    assert inprod.main() == 0
    assert [g.name for g in glasscat.load_catalog(tmp_path / "TEST_INPROD.AGF")] == ["H-K9L", "H-ZF52"]
    (tmp_path / "MAKER.AGF").write_bytes(raw + b"\x00\x00")  # 文件换了 / the file changed
    (tmp_path / "TEST_INPROD.AGF").unlink()
    assert inprod.main() == 1 and not (tmp_path / "TEST_INPROD.AGF").exists()
