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
