"""saos 材料集成的独立夹具；数值观测注明来源，不依赖本机玻璃库。

夹具镜头在 −40 °C、0 atm、打开 Adjust Index，目录玻璃走 catalog:N:thermal 路径。这些路径尚未准入，
所以材料算术类测试用 ``fast``（显式放开准入）；准入本身用 ``strict_*`` 测试按真实准入表检查。
"""

import copy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from saos.backend import rtc as rtc_backend
from saos.backend.rtc import RtcBackend
from saos.fast import FastLens
from saos.protocol import ErrorCode, SaosError


class _Everything:
    def __contains__(self, item):
        return True


AGF = """NM REF 2 0 1.5168 64.17
CD 1.03961212 .00600069867 .231792344 .0200179144 1.01046945 103.560653
TD 1e-6 2e-8 -1e-10 3e-7 4e-9 .17 20
LD .3 2.5
NM WARM 3 0 1.5 50
CD 1.5 .001 .0001 .0002 0 0
TD 1e-6 2e-8 -1e-10 3e-7 4e-9 -.17 25
LD .3 2.5
NM NARROW 1 0 1.5 50
CD 2.25 0 0 0 0 0
LD .3 .6
NM UNKNOWN 14 0 1.5 50
CD 2.25 0 0 0 0 0
LD .3 2.5
"""

ZMX = """VERS 190513
MODE SEQ
UNIT MM
ENVD -40 0 1
ENPD 10
GCAT TEST
FTYP 0 0 1 3 0
XFLN 0
YFLN 0
FWGN 1
WAVM 1 .36 1
WAVM 2 .55 1
WAVM 3 1.06 1
PWAV 1
SURF 0
  TYPE STANDARD
  DISZ INFINITY
SURF 1
  TYPE STANDARD
  CURV .01
  DISZ 2
  DIAM 5
  GLAS REF 0 0 1.5168 64.17 0
  STOP
SURF 2
  TYPE STANDARD
  CURV -.01
  DISZ 10
  DIAM 5
SURF 3
  TYPE STANDARD
  DISZ 0
  DIAM 5
"""


# 拒绝机制的测试用固定的小准入表，不随 rtc_admitted.json 扩充而变
NARROW = frozenset({"air", "catalog:1:fixed", "catalog:2:fixed", "model:fixed"})


@pytest.fixture
def lens_path(tmp_path):
    (tmp_path / "TEST.AGF").write_text(AGF, encoding="utf-8")
    path = tmp_path / "lens.zmx"
    path.write_text(ZMX, encoding="utf-8")
    return path


@pytest.fixture
def fast(lens_path, monkeypatch):
    monkeypatch.setattr(rtc_backend, "admitted_paths", lambda: _Everything())
    return FastLens(RtcBackend(lens_path, glass_dir=lens_path.parent))


def _description(backend):
    """与 OpticStudio describe_model 同形的描述；INDX 取 rtc 材料层的值（相当于 OpticStudio 与之一致）。"""
    from rtc.material import resolve

    lens = backend.lens
    environment = asdict(lens.environment)
    environment["adjust_index"] = environment.pop("adjust")

    def indices(s):
        medium = resolve(lens.surface(s["index"]).material, backend._catalog(), lens.catalogs)
        return [float(medium.index(um, lens.environment)) for um, _ in lens.active_wavelengths]

    return {
        "aperture": {"type": "EntrancePupilDiameter", "value": 10.0},
        "ray_aiming": "Off", "environment": environment, "stop_surface": 1,
        "fields": {"type": "Angle", "normalization": "radial", "items": [{"x": 0.0, "y": 0.0}]},
        "wavelengths": [{"um": um, "primary": i == 1} for i, (um, _) in enumerate(lens.active_wavelengths, 1)],
        "surfaces": [{**s, "params": [], "n": [1.0] * 3 if s["index"] == 0 else indices(s)} for s in backend.surfaces()],
    }


def test_warm_herzberger_swap_uses_parsed_environment(fast):
    fast.set({"1.material": "WARM"})
    assert fast.get("1.material") == "WARM"
    assert fast.backend.lens.environment.temperature == -40
    assert fast.backend.lens.environment.pressure == 0
    assert fast.backend.lens.environment.adjust is True
    assert all(fast.backend._system(w).items[0].n_after > 1 for w in (1, 2, 3))


@pytest.mark.parametrize("value,code", [("NARROW", ErrorCode.BACKEND_UNSUPPORTED), ("UNKNOWN", ErrorCode.BACKEND_UNSUPPORTED),
                                        ("MIRROR", ErrorCode.BACKEND_UNSUPPORTED), ("MISSING", ErrorCode.BAD_ARGS)])
def test_bad_material_preserves_state_and_existing_cached_system(fast, value, code):
    # 越过 LD、未实现的公式、反射镜是能力缺口；目录里没有的牌号是参数错误
    before = fast.backend._system(1)
    with pytest.raises(SaosError) as exc:
        fast.set({"1.material": value})
    assert exc.value.code == code
    assert fast.get("1.material") == "REF" and fast.changed == {}
    assert fast.backend._system(1) is before


@pytest.mark.parametrize("value", [{"nd": -1.0, "vd": 50}, {"nd": 1.5}, {"nd": 1.5, "vd": 50, "x": 1}])
def test_invalid_model_material_is_bad_args(fast, value):
    with pytest.raises(SaosError) as exc:
        fast.set({"1.material": value})
    assert exc.value.code == ErrorCode.BAD_ARGS and fast.get("1.material") == "REF"


def test_model_glass_uses_formula_without_lookup_table(fast):
    fast.set({"1.material": {"nd": 1.5168, "vd": 64.17, "dpgf": 0.0}})
    # rtc fixtures/material 的 ZOS-API 实测：λ=.36、T=-40、P=0、Adjust=1
    assert fast.backend._system(1).items[0].n_after == pytest.approx(1.5378765180018454, rel=0, abs=1e-14)
    fast.set({"1.nd": 1.637, "1.vd": 57.3, "1.dpgf": .027})
    assert fast.get("1.material") == {"nd": 1.637, "vd": 57.3, "dpgf": .027}
    assert fast.values(["1.nd", "1.vd", "1.dpgf"]) == {"1.nd": 1.637, "1.vd": 57.3, "1.dpgf": .027}


def test_kept_material_restores_catalog_instead_of_replacing_it_with_a_model(fast):
    with fast.kept(["1.material"]):
        fast.set({"1.nd": 1.65})
        assert isinstance(fast.get("1.material"), dict)
    assert fast.get("1.material") == "REF"


def test_model_material_push_preserves_all_three_parameters(fast):
    fast.set({"1.material": "WARM"})
    fast.set({"1.nd": 1.65, "1.vd": 52, "1.dpgf": .02})
    written = {}
    pushed = fast.push(SimpleNamespace(set=written.update))
    assert written == pushed
    assert written["1.material"] == {"nd": 1.65, "vd": 52, "dpgf": .02}


def test_push_merges_a_single_nd_change_into_the_whole_model(fast):
    # 只写 1.nd 时 OpticStudio 会用它自己目录的 Vd 补齐，与副本的 Vd 未必相同，所以整块写 material
    fast.set({"1.nd": 1.6})
    written = {}
    fast.push(SimpleNamespace(set=written.update))
    assert set(written) == {"1.material"}
    assert written["1.material"] == fast.get("1.material") and written["1.material"]["nd"] == 1.6


def test_surfaces_report_model_material_as_text(fast):
    fast.set({"1.material": {"nd": 1.6, "vd": 50, "dpgf": 0}})
    assert fast.backend.surfaces()[1]["material"] == "MODEL 1.6 50 0"


def test_strict_copy_refuses_unadmitted_paths_without_opticstudio_evidence(lens_path, monkeypatch):
    # 夹具环境下的 catalog:2:thermal 不在准入表里，也没有 OpticStudio INDX 可确认
    monkeypatch.setattr(rtc_backend, "admitted_paths", lambda: NARROW)
    with pytest.raises(SaosError) as exc:
        RtcBackend(lens_path, glass_dir=lens_path.parent)
    assert exc.value.code == ErrorCode.BACKEND_UNSUPPORTED and "catalog:2:thermal" in exc.value.message


def test_strict_copy_uses_indx_confirmation_but_refuses_unconfirmed_swaps(fast, lens_path, monkeypatch):
    model = _description(fast.backend)
    monkeypatch.undo()
    monkeypatch.setattr(rtc_backend, "admitted_paths", lambda: NARROW)
    strict = FastLens(RtcBackend(lens_path, model, glass_dir=lens_path.parent))
    assert all(n > 1 for n in (strict.backend._system(w).items[0].n_after for w in (1, 2, 3)))
    with pytest.raises(SaosError) as exc:
        strict.set({"1.material": "WARM"})
    assert exc.value.code == ErrorCode.BACKEND_UNSUPPORTED and "catalog:3:thermal" in exc.value.message
    assert strict.get("1.material") == "REF"
    # 已确认的材料在别的波长上没有证据
    with pytest.raises(SaosError) as exc:
        strict.set({"wave.2": .6})
    assert exc.value.code == ErrorCode.BACKEND_UNSUPPORTED and strict.get("wave.2") == .55


def test_wave_edit_validates_before_commit_and_air_swap_clears_model(fast):
    before = fast.backend._system(1)
    with pytest.raises(SaosError) as exc:
        fast.set({"wave.2": 3.0})
    assert exc.value.code == ErrorCode.BACKEND_UNSUPPORTED
    assert fast.get("wave.2") == .55 and fast.backend._system(1) is before
    fast.set({"1.material": {"nd": 1.65, "vd": 52, "dpgf": .02}})
    fast.set({"1.material": ""})
    assert fast.get("1.material") == ""
    assert fast.backend.lens.surface(1).model_nd is None
    assert fast.backend._system(1).items[0].n_after == 1.0


@pytest.mark.parametrize("field,value", [("temperature", 20), ("pressure", .5), ("adjust_index", False)])
def test_envd_is_compared_even_when_indx_matches(fast, field, value):
    model = _description(fast.backend)
    model["environment"][field] = value
    with pytest.raises(SaosError) as exc:
        RtcBackend(fast.backend.path, model, glass_dir=fast.backend.glasses.folder)
    assert exc.value.code == ErrorCode.VERIFY_FAILED
    assert "environment" in exc.value.message


def test_indx_guard_remains_and_requires_every_active_wavelength(fast):
    model = _description(fast.backend)
    bad = copy.deepcopy(model)
    bad["surfaces"][1]["n"][2] += 1e-6
    with pytest.raises(SaosError) as exc:
        RtcBackend(fast.backend.path, bad, glass_dir=fast.backend.glasses.folder)
    assert exc.value.code == ErrorCode.VERIFY_FAILED and "index" in exc.value.message
    bad = copy.deepcopy(model)
    bad["surfaces"][1]["n"].pop()
    with pytest.raises(SaosError) as exc:
        RtcBackend(fast.backend.path, bad, glass_dir=fast.backend.glasses.folder)
    assert exc.value.code == ErrorCode.VERIFY_FAILED and "INDX" in exc.value.message


@pytest.mark.parametrize("param,value", [("nd", float("nan")), ("vd", True), ("dpgf", float("inf"))])
def test_model_parameter_rejects_invalid_numbers_before_mutation(fast, param, value):
    with pytest.raises(SaosError) as exc:
        fast.set({f"1.{param}": value})
    assert exc.value.code == ErrorCode.BAD_ARGS and fast.get("1.material") == "REF"


def test_load_keeps_the_explicit_catalog_directory(fast):
    folder = fast.backend.glasses.folder
    fast.backend.load(fast.backend.path)
    assert fast.backend.glasses.folder == folder
