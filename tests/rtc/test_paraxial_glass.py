"""PARAXIAL 面后接玻璃：与 OpticStudio 实测对照（fixtures/golden/paraxial-glass，2026-10-28 由 saos 的 ZOS 后端建镜头、
批量追迹并读 PLEN、OPDC）。覆盖三点：出射规则 n'·tan u' = n·tan u − y/f 与面后折射率的传递；opl 与批量追迹 / PLEN 同口径，
不含理想透镜相位（相位在 RayBlock.phase）；加上相位后的 OPD 与 OPDC 一致。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from rtc import zmx
from rtc.psf import opd_map
from rtc.system import OpticalSystem
from rtc.trace import trace

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden" / "paraxial-glass"


@pytest.fixture(scope="module")
def case():
    lens = zmx.parse(GOLDEN / "lens.zmx")
    data = json.loads((GOLDEN / "trace.json").read_text(encoding="utf-8"))
    return OpticalSystem(lens, wavelength_um=data["wavelength_um"]), data


def test_rays_after_a_paraxial_surface_in_glass_match_opticstudio(case):
    system, data = case
    assert system.items[0].type == "PARAXIAL" and system.items[0].n_after == pytest.approx(data["n_after_paraxial"], abs=1e-12)
    for row in data["rays"]:
        got = trace(system, [row["px"]], [row["py"]], row["hx"], row["hy"]).at(row["surface"])
        for key in ("x", "y", "z"):
            assert got[key][0] == pytest.approx(row[key], abs=1e-12), (row, key)
        for key in ("l", "m", "n"):
            assert got[key][0] == pytest.approx(row[key], abs=1e-14), (row, key)


def test_opl_follows_the_batch_trace_and_keeps_the_lens_phase_apart(case):
    system, data = case
    for row in data["plen"]:
        block = trace(system, [row["px"]], [row["py"]], row["hx"], row["hy"])
        opl = block.at(row["to"])["opl"][0] - block.at(row["from"])["opl"][0]
        assert opl == pytest.approx(row["plen"], abs=1e-12), row
        assert block.at(row["to"])["phase"][0] == block.at(1)["phase"][0]
    assert np.all(block.phase[:, 0] == block.phase[:, -1])


def test_wavefront_after_a_paraxial_surface_in_glass_matches_opdc(case):
    # 同时覆盖：出瞳（近轴追迹在 PARAXIAL 面后换成玻璃折射率）、参考球面交点、理想透镜的精确相位与 OPD 符号
    system, data = case
    ref = data["opdc"]
    assert system.exit_pupil()[0] == pytest.approx(-40.0 + 1.0 / (0.5185223876207927 / 30.0 - 1.5185223876207927 / 5.0), abs=1e-9)
    opd, _, _, _ = opd_map(system, ref["px"], np.full(len(ref["px"]), ref["py"]), remove_tilt=False)
    assert opd == pytest.approx(ref["waves"], abs=1e-5)
