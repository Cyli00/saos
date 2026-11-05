"""rtc 副本（saos.backend.rtc、saos.fast）的离线回归，不开 OpticStudio。

基准是 tests/fixtures/rtc/：4 面双胶合（光阑在第 1 面、不瞄准）由 OpticStudio 24.1 存出的副本、它的 describe_model
和 OpticStudio 的批量追迹结果，由 tests/test_zemax_rtc.py::test_record_offline_fixture 生成。玻璃取本机
Documents/Zemax/Glasscat（与 OpticStudio 同一份 AGF），找不到 SCHOTT.AGF 时跳过。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from saos.backend.rtc import RtcBackend
from saos.fast import FastLens, from_context
from saos.protocol import ErrorCode, SaosError

FIXTURES = Path(__file__).parent / "fixtures" / "rtc"
LENS = FIXTURES / "doublet.zmx"


def _glasscat_ready() -> bool:
    from rtc.materials import catalog_dir

    return any(p.stem.upper() == "SCHOTT" for p in catalog_dir().glob("*.[Aa][Gg][Ff]"))


pytestmark = pytest.mark.skipif(not _glasscat_ready(), reason="需要本机 Documents/Zemax/Glasscat/SCHOTT.AGF")


@pytest.fixture(scope="module")
def recorded():
    return json.loads((FIXTURES / "doublet.json").read_text(encoding="utf-8"))


@pytest.fixture
def fast(recorded):
    return FastLens(RtcBackend(LENS, copy.deepcopy(recorded["model"])))


def _zmx_text(tmp_path: Path, old: str, new: str) -> Path:
    from rtc.textio import decode_text

    text = decode_text(LENS.read_bytes())
    assert old in text
    out = tmp_path / "edited.zmx"
    out.write_text(text.replace(old, new, 1), encoding="utf-8")
    return out


def test_copy_reproduces_the_recorded_opticstudio_rays(fast, recorded):
    rows = recorded["rays"]
    for to, ref in recorded["traces"].items():
        got = fast.trace(rows, int(to))
        assert (got.error == np.array(ref["error"])).all() and (got.vignette == np.array(ref["vignette"])).all()
        for k in ("x", "y", "z"):
            assert np.max(np.abs(getattr(got, k) - np.array(ref[k]))) < 1e-10, k
        for k in ("l", "m", "n"):
            assert np.max(np.abs(getattr(got, k) - np.array(ref[k]))) < 1e-12, k
    # 点列 RMS 用的是与 ctx.spot 同一份 kit 代码 / the spot RMS runs the same kit code as ctx.spot
    rms = [r["rms_radius"] for r in fast.spot([(0.0, 0.0), (0.0, 0.5), (0.0, 1.0), (0.3, -0.7)], wave=[1, 2, 3], grid=21)]
    assert rms == pytest.approx(recorded["spot_rms"], rel=1e-9)
    assert fast.backend.identity() == {"backend": "rtc", "version": fast.backend.identity()["version"], "precision": "float64"}


def test_parameters_round_trip_and_change_the_trace(fast):
    before = fast.trace([(2, 0.0, 1.0, 0.0, 1.0)]).y[0]
    with fast.kept(["2.radius", "3.par2"]):
        fast.set({"2.radius": -46.0, "3.par2": 5e-7})
        assert fast.get("2.radius") == -46.0 and fast.get("2.curvature") == pytest.approx(-1 / 46.0)
        assert fast.get("3.par2") == 5e-7
        assert fast.trace([(2, 0.0, 1.0, 0.0, 1.0)]).y[0] != before
    assert fast.trace([(2, 0.0, 1.0, 0.0, 1.0)]).y[0] == before
    assert set(fast.changed) == {"2.radius", "3.par2"}
    with pytest.raises(SaosError) as err:
        fast.set({"1.material": "NO-SUCH-GLASS"})
    # 在用目录里没有的牌号是参数错误（与 ZOS 后端一致），不是 rtc 的能力缺口
    assert err.value.code == ErrorCode.BAD_ARGS
    with pytest.raises(SaosError) as err:
        fast.op("EFFL")
    assert err.value.code == ErrorCode.BACKEND_UNSUPPORTED


def test_push_writes_the_changed_values_through_ctx_set(fast):
    fast.set({"2.radius": -46.0})
    written = {}
    pushed = fast.push(SimpleNamespace(set=written.update))
    assert pushed == written == {"2.radius": -46.0}


def test_solves_rtc_does_not_recompute_hold_their_parameters(recorded):
    model = copy.deepcopy(recorded["model"])
    model["surfaces"][3]["solves"]["thickness"] = "MarginalRayHeight"
    strict = FastLens(RtcBackend(LENS, model))
    assert strict.held == {"3.thickness": "MarginalRayHeight"}
    with pytest.raises(SaosError) as err:
        strict.set({"2.radius": -46.0})
    assert err.value.code == ErrorCode.BACKEND_UNSUPPORTED
    frozen = FastLens(RtcBackend(LENS, model, freeze_solves=True))
    frozen.set({"2.radius": -46.0})
    with pytest.raises(SaosError) as err:
        frozen.set({"3.thickness": 90.0})
    assert err.value.code == ErrorCode.VALUE_NOT_APPLIED


def test_settings_rtc_cannot_reproduce_are_refused(recorded, tmp_path):
    tilted = copy.deepcopy(recorded["model"])
    tilted["surfaces"][2]["tilt_decenter"] = {"before_TiltX": 1.0}
    with pytest.raises(SaosError) as err:
        RtcBackend(LENS, tilted)
    assert err.value.code == ErrorCode.BACKEND_UNSUPPORTED and "tilt" in err.value.message
    for old, new, word in (# 物方 NA 的发射与瞄准没对上 OpticStudio（piB 差 0.096 mm）/ object space NA does not match OpticStudio
                           ("ENPD 10", "OBNA 0.05", "entrance pupil diameter"),
                           ("TYPE EVENASPH", "TYPE BINARY_2", "not reconciled"),
                           # 读得懂不等于放行：这些孔径只有候选验收 / readable apertures stay refused until reconciled
                           ("TYPE EVENASPH", "TYPE EVENASPH\n  ELAP 2 1 0", "aperture code 6"),
                           ("TYPE EVENASPH", "TYPE EVENASPH\n  SPID 0.5 4 0", "aperture code 3")):
        with pytest.raises(SaosError) as err:
            RtcBackend(_zmx_text(tmp_path, old, new))
        assert err.value.code == ErrorCode.BACKEND_UNSUPPORTED and word in err.value.message, (new, err.value.message)


def test_only_standard_mirrors_without_ray_aiming_are_admitted(tmp_path):
    mirror = _zmx_text(tmp_path, "GLAS N-SF2", "GLAS MIRROR")
    assert RtcBackend(mirror).lens.surface(2).mirror
    mirror.write_text(mirror.read_text(encoding="utf-8").replace("RAIM 0 0", "RAIM 0 2", 1), encoding="utf-8")
    with pytest.raises(SaosError) as err:
        RtcBackend(mirror)
    assert err.value.code == ErrorCode.BACKEND_UNSUPPORTED and "real ray aiming" in err.value.message


def test_swaps_follow_the_material_admission_table(tmp_path, monkeypatch):
    # 用固定的小准入表测拒绝机制，不随 rtc_admitted.json 扩充而变
    monkeypatch.setattr("saos.backend.rtc.admitted_paths",
                        lambda: frozenset({"air", "catalog:1:fixed", "catalog:2:fixed", "model:fixed"}))
    fast = FastLens(RtcBackend(_zmx_text(tmp_path, "GCAT SCHOTT", "GCAT SCHOTT OHARA SAOS_CDGM_INPROD")))
    fast.set({"3.material": "N-SF5"})
    # OHARA 的 25 °C 参考玻璃在 Adjust Index 关闭时走 catalog:2:fixed，已准入
    fast.set({"1.material": "S-BSL7"})
    assert fast.get("1.material") == "S-BSL7"
    assert all(np.isfinite(fast.backend._system(w).items[0].n_after) for w in (1, 2, 3))
    # Herzberger（公式 3）不在这张准入表里：拒绝，副本不变
    with pytest.raises(SaosError) as err:
        fast.set({"1.material": "HWS1"})
    assert err.value.code == ErrorCode.BACKEND_UNSUPPORTED and "catalog:3:fixed" in err.value.message
    assert fast.get("1.material") == "S-BSL7"


def test_a_copy_that_differs_from_opticstudio_is_rejected(recorded):
    model = copy.deepcopy(recorded["model"])
    model["surfaces"][2]["radius"] = -44.0
    with pytest.raises(SaosError) as err:
        RtcBackend(LENS, model)
    assert err.value.code == ErrorCode.VERIFY_FAILED and "surface 2 radius" in err.value.message
    model = copy.deepcopy(recorded["model"])
    model["surfaces"][1]["n"][0] += 1e-6
    with pytest.raises(SaosError) as err:
        RtcBackend(LENS, model)
    assert err.value.code == ErrorCode.VERIFY_FAILED and "index" in err.value.message


def test_checks_and_backends_without_export_get_no_copy():
    from saos.backend.fake import FakeBackend
    from saos.scripting import ScriptContext

    check = ScriptContext(SimpleNamespace(backend=FakeBackend()), None, {}).child({})
    with pytest.raises(SaosError) as err:
        check.fast()
    assert err.value.code == ErrorCode.BAD_ARGS
    with pytest.raises(SaosError) as err:
        from_context(SimpleNamespace(backend=FakeBackend(), in_check=False))
    assert err.value.code == ErrorCode.BACKEND_UNSUPPORTED


def test_a_check_reaching_for_ctx_system_on_rtc_is_filed_as_a_backend_gap():
    # 10-30 的光纤任务里 6 个检查直接读 ctx.system.LDE，在 rtc 上一项都算不了 / six checks read ctx.system.LDE directly
    from saos import demand
    from saos.scripting import ScriptContext

    ctx = ScriptContext(SimpleNamespace(backend=RtcBackend(None, standalone=True)), None, {}).child({})
    with pytest.raises(SaosError) as err:
        ctx.system
    assert err.value.code == ErrorCode.BACKEND_UNSUPPORTED and "ctx.get" in err.value.message
    assert demand._error_kind(err.value) == "backend"
    assert demand._error_kind(ValueError("bad geometry")) == "computation"


def test_psf_and_wavefront_wait_for_reconciliation_on_rtc_while_apertures_read_as_recorded(fast, recorded):
    # rtc.psf.psf_huygens 已有，但没和 OpticStudio 对账前不给数，判定归为后端缺口 / no numbers before reconciliation
    from saos import demand, kit

    with pytest.raises(SaosError) as err:
        with kit.huygens(fast):
            pass
    assert err.value.code == ErrorCode.BACKEND_UNSUPPORTED and demand._error_kind(err.value) == "backend"
    with pytest.raises(SaosError) as err:
        kit.wavefront_rms(fast, (0.0, 0.0))
    assert err.value.code == ErrorCode.BACKEND_UNSUPPORTED
    assert [kit.aperture(fast, s["index"]) for s in fast.backend.surfaces()] == [s["aperture"] for s in recorded["model"]["surfaces"]]
    with pytest.raises(SaosError) as err:
        kit.aperture(fast, 9)
    assert err.value.code == ErrorCode.BAD_ARGS
