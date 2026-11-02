"""saos.micro 里不需要后端的部分：焦点闭式解、介质里的前进距离、借池结果的解读、变焦行程扫描。
The parts of saos.micro that need no backend: the closed-form focus, travelled distance in a medium, reading pooled
results, and the zoom-travel sweep."""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from saos import micro


def _converging(z0: float, n: int = 9) -> list[tuple[float, ...]]:
    """在像面上 (x, y)、方向指向 (0, 0, z0) 的直线光线：最佳焦点就在 z0。/ Straight rays aimed at (0, 0, z0)."""
    rows = []
    for k in range(n):
        x, y = 0.01 * math.cos(2 * math.pi * k / n), 0.01 * math.sin(2 * math.pi * k / n)
        d = (-x, -y, z0)
        norm = math.sqrt(sum(c * c for c in d))
        rows.append((x, y, 0.0, d[0] / norm, d[1] / norm, d[2] / norm))
    return rows


def test_best_focus_is_where_the_rays_meet_and_a_parallel_beam_has_none():
    assert micro.best_focus_dz(_converging(0.25)) == pytest.approx(0.25, rel=1e-12)
    assert micro.rms_radius(_converging(0.25), 0.25) == pytest.approx(0.0, abs=1e-15)
    parallel = [(0.01 * k, 0.0, 0.0, 0.0, 0.0, 1.0) for k in range(5)]
    with pytest.raises(micro.DesignFailure):
        micro.best_focus_dz(parallel)
    with pytest.raises(micro.DesignFailure):
        micro.centroid([])


def _ctx_with(values: dict[str, float]):
    backend = SimpleNamespace(get_param=lambda key: values[key], set_param=lambda key, value: values.__setitem__(key, value))
    return SimpleNamespace(backend=backend)


def test_a_folded_medium_counts_the_travelled_distance_exactly_as_negative_thicknesses():
    # 侧视探头折转一次后厚度为负：前进距离、焦点深度与写回的厚度都要和原来的 -t、d - dz、-d 逐位相同
    values = {"7.thickness": -0.0312345678901}
    ctx = _ctx_with(values)
    tissue = micro.Medium("7.thickness", -1)
    dz = 0.0071234567
    assert tissue.distance(ctx) == -values["7.thickness"]
    assert tissue.depth(ctx, dz) == -values["7.thickness"] - dz
    tissue.place(ctx, 0.042)
    assert values["7.thickness"] == -0.042
    forward = micro.Medium("3.thickness", 1)
    assert forward.depth(_ctx_with({"3.thickness": 0.3}), dz) == 0.3 + dz


def test_pooled_results_keep_finished_items_and_raise_a_design_failure_from_any_worker():
    out = {"results": [1.5, None, 2.5], "errors": {}, "not_run": [1]}
    ctx = SimpleNamespace(spare_map=lambda fn, items, **kw: out)
    assert micro.pooled(ctx, "saos.x:f", [{}, {}, {}]) == {0: 1.5, 2: 2.5}
    failing = SimpleNamespace(spare_map=lambda fn, items, **kw: {"results": [None], "errors": {"0": "Traceback ...\nDesignFailure: no focus"}, "not_run": []})
    with pytest.raises(micro.DesignFailure, match="no focus"):
        micro.pooled(failing, "saos.x:f", [{}])
    # 不在后台进程里（worker 内、离线）没有 spare_map：返回空，由调用方串行算 / no spare_map: empty, the caller goes serial
    assert micro.pooled(SimpleNamespace(), "saos.x:f", [{}]) == {}
    assert micro.pooled(SimpleNamespace(spare_map=lambda fn, items, **kw: None), "saos.x:f", [{}]) == {}


def test_focus_travel_reports_the_ends_and_refuses_a_span_with_unreadable_points():
    state = {"q": 0.0}

    def apply(q):
        state["q"] = q

    linear = micro.focus_travel(None, apply, 0.02, 0.22, lambda ctx: 0.2 + 1.5 * state["q"], n=9)
    assert linear["near"] == pytest.approx(0.2 + 1.5 * 0.02) and linear["far"] == pytest.approx(0.2 + 1.5 * 0.22)
    assert linear["span"] == pytest.approx(1.5 * 0.2) and linear["monotonic"] and linear["coverage_ok"]

    def depth(ctx):
        if state["q"] > 0.15:
            raise micro.DesignFailure("virtual focus")
        return 0.2 + state["q"]

    broken = micro.focus_travel(None, apply, 0.02, 0.22, depth, n=9)
    assert not broken["coverage_ok"] and broken["span"] is None
    assert any(v is None for _, v in broken["samples"])


def test_guarded_turns_invalid_states_into_inf():
    assert micro.guarded(lambda ctx: 1.25)(None) == 1.25
    assert micro.guarded(lambda ctx: float("nan"))(None) == math.inf
    assert micro.guarded(lambda ctx: (_ for _ in ()).throw(micro.DesignFailure("x")))(None) == math.inf


def test_bad_sampling_and_the_image_as_front_are_caller_errors_that_guarded_does_not_hide():
    # 采样数非法会让零件被测成 0 或除零；属调用错误，必须是 BAD_ARGS，不能经 guarded 变成 inf 或静默放行
    from saos.protocol import ErrorCode, SaosError

    rows = [{"index": k, "type": "STANDARD"} for k in range(3)]
    backend = SimpleNamespace(surface_count=lambda: len(rows), surfaces=lambda: rows)
    ctx = SimpleNamespace(backend=backend)
    axis = micro.Axis((0.0, 0.0, 0.0), (0.0, 0.0, 1.0))
    calls = {
        "image as front": lambda: micro.min_gap(ctx, -1),
        "one gap ring": lambda: micro.min_gap(ctx, 0, rings=1),
        "zero extent rings": lambda: micro.radial_extent(ctx, rows, axis, rings=0),
        "zero extent spokes": lambda: micro.radial_extent(ctx, rows, axis, spokes=0),
        "fractional rings": lambda: micro.radial_extent(ctx, rows, axis, rings=2.5),
        "zero NA rays": lambda: micro.marginal_na(ctx, 1, rays=0),
        "one-point collection grid": lambda: micro.collection_fraction(ctx, [(0.0, 0.0)], 1, n=1),
        "collection grid without a whole cell": lambda: micro.collection_fraction(ctx, [(0.0, 0.0)], 1, n=3),
        "no collection field": lambda: micro.collection_fraction(ctx, [], 1),
    }
    for name, call in calls.items():
        with pytest.raises(SaosError) as exc:
            call()
        assert exc.value.code == ErrorCode.BAD_ARGS, name
        with pytest.raises(SaosError):
            micro.guarded(lambda _ctx: call())(None)
