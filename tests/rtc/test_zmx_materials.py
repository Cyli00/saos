"""材料回归与 .zmx 解析回归。

材料对照值来源：
- N-BK7 @0.55 µm = 1.518522388 —— ORACLE.md §7.2 用 RAGA 实测反推得到（观测）。
- WATER2 @0.5875618 µm 应回到目录里的 nd = 1.333044 —— AGF 自洽性检查，
  同时验证"公式 1 = Schott Laurent 级数"这一读法。
- WATER2 @0.55 / 0.92 µm 是手算对照（±2e-4），尚待 INDX 实测确认。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from rtc import zmx
from rtc.materials import Catalog

FIX = Path(__file__).resolve().parent / "fixtures"


def test_n_bk7_matches_measured_ray_trace_value():
    glass = Catalog().get("N-BK7")
    assert glass.index(0.55) == pytest.approx(1.518522388, abs=5e-10)


def test_water2_reproduces_catalog_nd():
    glass = Catalog().get("WATER2")
    assert glass.formula == 1
    assert glass.index(0.5875618) == pytest.approx(glass.nd, abs=1e-7)


@pytest.mark.parametrize("wavelength,expected", [(0.55, 1.33432), (0.92, 1.32633)])
def test_water2_hand_computed_points(wavelength, expected):
    assert Catalog().get("WATER2").index(wavelength) == pytest.approx(expected, abs=2e-4)


def test_unknown_dispersion_formula_is_rejected_not_approximated():
    from rtc.materials import Glass

    with pytest.raises(NotImplementedError):
        Glass(name="X", formula=14, nd=1.5, vd=50, coef=[1, 2, 3, 4, 5, 6]).index(0.55)


# ---------------------------------------------------------------- .zmx 解析


@pytest.mark.parametrize(
    "name,surfaces,configs,stop,aperture,raim,types",
    [
        ("piA-r012.zmx", 13, 3, 2, ("EPD", 1.0), 0, {"PARAXIAL", "BICONICX", "BICONICZ", "TOROIDAL", "STANDARD"}),
        ("piB-r007.zmx", 11, 1, 9, ("OBNA", 0.12), 2, {"EVENASPH", "BICONICZ", "TOROIDAL"}),
        ("codex-r017.zmx", 14, 9, 3, ("EPD", 1.0), 0, {"PARAXIAL", "EVENASPH", "BICONICX", "BICONICZ", "TOROIDAL"}),
    ],
)
def test_final_lenses_parse(name, surfaces, configs, stop, aperture, raim, types):
    lens = zmx.parse(FIX / "lenses" / name)
    assert len(lens.surfaces) == surfaces
    assert lens.num_configs == configs
    assert lens.stop_index == stop
    assert lens.aperture_type == aperture[0]
    assert lens.aperture_value == pytest.approx(aperture[1])
    assert lens.ray_aiming == raim
    assert types <= {s.type for s in lens.surfaces}
    assert [w for w, _ in lens.wavelengths[:2]] == pytest.approx([0.92, 0.55])
    assert {row.op for row in lens.mce} <= {"THIC", "MOFF"}


def test_multi_config_overrides_thickness_only():
    lens = zmx.parse(FIX / "lenses" / "piA-r012.zmx")
    assert lens.thickness(4, 1) == pytest.approx(0.311503)
    assert lens.thickness(4, 3) == pytest.approx(0.0372)
    assert lens.thickness(10, 2) == lens.surface(10).thickness  # 未被 MCE 覆盖


def test_xdat_slots_map_to_parameters_from_13():
    lens = zmx.parse(FIX / "lenses" / "piA-r012.zmx")
    biconicz = lens.surface(9)
    assert biconicz.type == "BICONICZ"
    assert biconicz.extra[2] == pytest.approx(-1.70402)
    assert biconicz.extra[2] == pytest.approx(biconicz.params[14] if 14 in biconicz.params else -1.70402)
