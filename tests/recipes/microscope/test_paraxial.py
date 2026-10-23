from __future__ import annotations

import math

import pytest

from saos.recipes.microscope.paraxial import Surface, back_focal_distance, effective_focal_length, front_focal_distance


def test_thick_biconvex_lens():
    # r1 = 50, r2 = -50, d = 5, n = 1.5：
    # 1/f = (n-1)(1/r1 - 1/r2 + (n-1)d/(n r1 r2))，BFD = f(1 - (n-1)d/(n r1))
    lens = [Surface(50.0, 5.0, 1.5), Surface(-50.0, 0.0, 1.0)]
    f = 1 / (0.5 * (1 / 50 + 1 / 50 - 0.5 * 5 / (1.5 * 50 * -50) * -1))
    assert effective_focal_length(lens) == pytest.approx(f, rel=1e-12)
    bfd = f * (1 - 0.5 * 5 / (1.5 * 50))
    assert back_focal_distance(lens) == pytest.approx(bfd, rel=1e-12)
    assert front_focal_distance(lens) == pytest.approx(bfd, rel=1e-12)


def test_plano_convex_orientation():
    # 平凸透镜，平面朝物：前焦距 = f - d/n，后焦距 = f
    lens = [Surface(math.inf, 4.0, 1.6), Surface(-30.0, 0.0, 1.0)]
    f = 30 / 0.6
    assert effective_focal_length(lens) == pytest.approx(f)
    assert back_focal_distance(lens) == pytest.approx(f)
    assert front_focal_distance(lens) == pytest.approx(f - 4.0 / 1.6)


def test_paraxial_check_agrees_with_the_recorded_opticstudio_reproduction():
    """近轴预筛与 OpticStudio 的复现记录一致（2026-10-15 核对 25 个收录条目：EFL 相对差 ≤ 5.4e-4，工作距离 ≤ 0.0134 mm）。
    差距来自复现时用的是匹配到的真实目录玻璃；比 library check 的 1% 判据小一个数量级，所以保留这段自写近轴计算（ADR-0012 的例外）。
    The paraxial pre-check agrees with the recorded OpticStudio reproductions of all admitted entries."""
    import json

    from saos.recipes.microscope import library

    entries = sorted(p for p in library.library_dir().joinpath("entries").iterdir() if (p / "repro.json").is_file())
    assert len(entries) >= 25
    for d in entries:
        report = json.loads((d / "adapt_report.json").read_text(encoding="utf-8"))
        repro = {m["metric"]: m["measured"] for m in json.loads((d / "repro.json").read_text(encoding="utf-8"))["metrics"]}
        p, cover = report["paraxial"], report.get("cover_glass")
        wd = p["front_focal_distance_mm"] - (cover["thickness"] / cover["n"] if cover else 0.0)
        assert abs(p["efl_mm"] / repro["efl"] - 1) < 1e-3, d.name
        assert abs(wd / repro["wd"] - 1) < 2e-3, d.name
