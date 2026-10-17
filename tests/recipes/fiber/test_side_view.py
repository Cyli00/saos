"""光纤配方测量库的离线测试：判定入口、无效值、同一次判定内只算一次。真机等价核对见 docs/recipes/fiber/。"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from saos.recipes.fiber import side_view as sv


def test_check_names_cover_the_printed_relay_demand():
    expected = {"fov", "working_distance", "focus_shift", "lens_index", "lens_base", "wall_clearance", "spot", "spot_axis", "spot_mid",
                "spot_edge", "pass_rate", "fov_resolved", "psf_x", "psf_y", "ee80", "psf_z", "wfe_rms", "collect_center", "collect_mid",
                "collect_edge"}
    assert set(sv.CHECKS) == expected
    with pytest.raises(ValueError):
        sv.check(SimpleNamespace(cache={}), "nope")


def test_design_failures_become_inf_and_are_remembered():
    ctx = SimpleNamespace(cache={})
    calls = []

    def failing():
        calls.append(1)
        raise sv.DesignFailure("no real focus")

    for _ in range(3):
        with pytest.raises(sv.DesignFailure):
            sv._once(ctx, ("x",), failing)
    assert len(calls) == 1  # 失败也只算一次 / a failure is computed once too
    assert sv.guarded(lambda c: sv._once(c, ("x",), failing))(ctx) == math.inf
    assert sv.guarded(lambda c: float("nan"))(ctx) == math.inf and sv.guarded(lambda c: 2.5)(ctx) == 2.5
    assert sv._once(ctx, ("y",), lambda: 7) == 7 and sv._once(ctx, ("y",), lambda: 8) == 7


def test_layout_is_hashable_for_the_cache():
    assert hash(sv.DEFAULT) == hash(sv.Layout()) and sv.Layout(object_height=0.1) != sv.DEFAULT
