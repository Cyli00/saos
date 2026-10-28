"""SAOS_RUN_ZEMAX=1 时在真实 OpticStudio 里搭出转换后的合成 .seq：波长设为 d 线后模型玻璃的折射率就是 nd，
EFFL 应与 seq.paraxial 一致，PIM 求解后的像距应与近轴像距一致（含反射面和变焦位置）。"""
from __future__ import annotations

import copy
import os

import pytest

from saos import prescription as rx
from saos.starts import seq

from test_patents import SINGLET, ZOOM, HEAD

pytestmark = [pytest.mark.zemax, pytest.mark.skipif(os.environ.get("SAOS_RUN_ZEMAX") != "1", reason="需要显式启用真实 OpticStudio 回归")]

MANGIN = HEAD + """S     0.0 20.0
  STO
S     -200.0 3.0 516800.641700
S     -220.0 -3.0 REFL
S     -200.0 -60.0
  PIM
SI    0.0 0.0
GO
"""


@pytest.fixture(scope="module")
def zos():
    from saos.backend.zos import ZosBackend
    backend = ZosBackend()
    try:
        backend.start()
        yield backend
    finally:
        backend.close()


@pytest.mark.parametrize("text,position", [(SINGLET, None), (ZOOM, 1), (ZOOM, 2), (MANGIN, None)])
def test_converted_lens_matches_the_paraxial_prescreen(zos, text, position):
    data = seq.parse(text, "synthetic")
    p = copy.deepcopy(seq.to_prescription(data, position=position)["prescription"])
    p["wavelengths"] = {"um": [0.5875618], "primary": 1}
    zos.build(rx.normalize(p))
    px = seq.paraxial(data, position)
    assert zos.operand("EFFL", 0, 1, []) == pytest.approx(px["efl"], rel=1e-5)
    last = len(p["surfaces"]) - (1 if data["image"]["thickness"] else 0)
    assert float(zos.system.LDE.GetSurfaceAt(last).Thickness) == pytest.approx(px["image_distance"], rel=1e-5, abs=1e-6)
