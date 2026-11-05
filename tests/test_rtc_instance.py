"""rtc 实例（SAOS_BACKEND=rtc，需求阶段的可行性核算）的离线回归，不开 OpticStudio。

对照值来自 tests/fixtures/cases/：sequential.json 处方、OpticStudio 24.1.0 用它建出并保存的 sequential.zmx（自动半口径、
边缘光线高度求解后的厚度都在文件里），以及 reference.json 里在同一镜头上实测的 EFFL、TOTR、REAY 与点列。
玻璃取本机 Documents/Zemax/Glasscat（与 OpticStudio 同一份 AGF），找不到 SCHOTT.AGF 时跳过用目录玻璃的测试。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from saos import kit, prescription
from saos.backend.rtc import RtcBackend
from saos.daemon import OPTICSTUDIO_ONLY, Daemon, compare_surfaces
from saos.protocol import ErrorCode, SaosError

CASES = Path(__file__).parent / "fixtures" / "cases"


def _glasscat_ready() -> bool:
    from rtc.materials import catalog_dir

    return any(p.stem.upper() == "SCHOTT" for p in catalog_dir().glob("*.[Aa][Gg][Ff]"))


needs_glass = pytest.mark.skipif(not _glasscat_ready(), reason="需要本机 Documents/Zemax/Glasscat/SCHOTT.AGF")


def _case(name: str = "sequential") -> dict:
    return json.loads((CASES / f"{name}.json").read_text(encoding="utf-8"))


def _built(raw: dict) -> RtcBackend:
    backend = RtcBackend(None, standalone=True)
    backend.build(prescription.normalize(raw))
    return backend


def _singlet(**changes) -> dict:
    """模型玻璃单透镜：物在 50 mm、物方 NA 0.1、物高视场，最后厚度用边缘光线高度求解对焦。不用目录玻璃，不依赖本机 AGF。"""
    raw = {"title": "finite singlet", "aperture": {"type": "object_na", "value": 0.1},
           "fields": {"type": "object_height", "y": [0, 1.0]}, "wavelengths": {"um": [0.5876]}, "ray_aiming": "off",
           "object": {"thickness": 50},
           "surfaces": [{"radius": 30, "thickness": 5, "material": {"model": {"nd": 1.5168, "vd": 64.17}}, "stop": True},
                        {"radius": -30, "thickness": 40, "solves": {"thickness": {"type": "marginal_ray_height", "height": 0}}}]}
    raw.update(changes)
    return raw


@needs_glass
def test_built_case_matches_opticstudio_measurements():
    ref = json.loads((CASES / "reference.json").read_text(encoding="utf-8"))["cases"]["sequential"]
    backend = _built(_case())
    image = len(backend.surfaces()) - 1
    assert backend.operand("EFFL", 0, 0, []) == pytest.approx(ref["EFFL"], rel=1e-9)
    assert backend.operand("TOTR", 0, 0, []) == pytest.approx(ref["TOTR"], rel=1e-12)
    assert backend.operand("REAY", image, 0, [0, 0, 0, 1]) == pytest.approx(ref["REAY_marginal"], abs=1e-12)
    rows = kit.spot(SimpleNamespace(backend=backend), fields=((0, 0), (0, 1)), grid=11)
    for got, want in zip(rows, ref["spot"]):
        assert got["pass_rate"] == want["pass_rate"]
        assert got["rms_radius"] == pytest.approx(want["rms_radius"], rel=1e-9)
        assert got["geo_radius"] == pytest.approx(want["geo_radius"], rel=1e-9)
        assert got["chief"] == pytest.approx(want["chief"], abs=1e-12)


@needs_glass
def test_solves_and_automatic_semi_diameters_match_the_opticstudio_file():
    from rtc import zmx

    saved = zmx.parse(CASES / "sequential.zmx")
    backend = _built(_case())
    for want, got in zip(saved.surfaces, backend.surfaces()):
        assert got["thickness"] == pytest.approx(want.thickness, rel=1e-14, abs=1e-14)
        assert got["semi_diameter"] == pytest.approx(want.semi_diameter, rel=1e-12, abs=1e-12)


@needs_glass
def test_saved_lens_reloads_unchanged_and_keeps_its_solve(tmp_path):
    backend = _built(_case())
    before = backend.surfaces()
    backend.save_as(tmp_path / "feasibility.zmx")
    assert not backend.needs_save()
    backend.load(tmp_path / "feasibility.zmx")
    assert compare_surfaces(before, backend.surfaces()) == []
    # 求解随文件保存：改前面的曲率，像距跟着重算 / the solve is saved: changing a curvature recomputes the image distance
    backend.set_param("1.radius", 60.0)
    assert backend.needs_save()
    assert backend.surfaces()[3]["thickness"] != pytest.approx(before[3]["thickness"], rel=1e-6)
    with pytest.raises(SaosError) as exc:
        backend.set_param("3.thickness", 90.0)
    assert exc.value.code == ErrorCode.VALUE_NOT_APPLIED


def test_finite_object_solve_follows_the_paraxial_imaging_equation():
    backend = _built(_singlet())
    n = backend.operand("INDX", 1, 0, [])
    c1, c2, t, d = 1 / 30, -1 / 30, 5.0, 50.0
    # 近轴光线逐面：物点出发，经两面折射，求像距 / paraxial ray from the object point through both surfaces
    y, u = 1.0, 1.0 / d
    u = (u - y * c1 * (n - 1)) / n
    y += u * t
    u = n * u - y * c2 * (1 - n)
    assert backend.surfaces()[2]["thickness"] == pytest.approx(-y / u, rel=1e-12)


def test_finite_object_marginal_ray_has_the_object_space_na():
    backend = _built(_singlet())
    # 光阑在第 1 面：瞄准关时轴上边缘光线从物点射向近轴入瞳边缘，sinθ = NA（手册 §14.32）
    y1 = backend.operand("REAY", 1, 0, [0, 0, 0, 1])
    z1 = backend.operand("REAZ", 1, 0, [0, 0, 0, 1])
    assert y1 / math.hypot(y1, 50.0 + z1) == pytest.approx(0.1, rel=1e-12)
    assert "object space NA aperture" in backend.unverified()


def test_quick_focus_moves_the_image_to_the_smallest_rms_spot():
    raw = _singlet()
    del raw["surfaces"][1]["solves"]
    backend = _built(raw)
    ctx = SimpleNamespace(backend=backend)

    def rms():
        rows = kit.spot(ctx, fields=((0, 0), (0, 1)), grid=21)
        return sum(r["rms_radius"] ** 2 for r in rows)

    out = backend.quick_focus()
    best = rms()
    for dz in (-1e-3, 1e-3):
        backend.set_param("2.thickness", out["thickness_after"] + dz)
        assert rms() > best


def test_prescription_items_rtc_cannot_build_are_listed_together():
    # 反射面、矩形孔径与渐晕因子已放行；蜘蛛等孔径未对账，和膜层、未实现的面型一起拒绝
    raw = _singlet()
    raw["surfaces"][0]["coating"] = "AR"
    raw["surfaces"][1]["type"] = "oddasphere"
    # 同一面只报第一个问题，蜘蛛孔径单独放一面
    raw["surfaces"].insert(1, {"radius": "inf", "thickness": 1, "aperture": {"type": "spider", "arms": 4, "width": 1}})
    with pytest.raises(SaosError) as exc:
        _built(raw)
    assert exc.value.code == ErrorCode.BACKEND_UNSUPPORTED
    problems = " | ".join(exc.value.details["problems"])
    assert "coating" in problems and "oddasphere" in problems and "spider" in problems


def _mirror_lens(**changes) -> dict:
    raw = {"title": "mirror", "aperture": {"type": "entrance_pupil_diameter", "value": 20}, "fields": {"type": "angle", "y": [0]},
           "wavelengths": {"um": [0.5876]}, "ray_aiming": "off", "object": {"thickness": "inf"},
           "surfaces": [{"radius": -200, "conic": -1, "thickness": -100, "material": "MIRROR", "stop": True}]}
    raw.update(changes)
    return raw


@pytest.mark.parametrize("raw, words", [
    (_mirror_lens(surfaces=[{"type": "evenaspheric", "radius": -200, "thickness": -100, "material": "MIRROR", "stop": True}]), "mirror"),
])
def test_paths_without_opticstudio_truth_are_refused(raw, words):
    with pytest.raises(SaosError) as exc:
        _built(raw)
    assert exc.value.code == ErrorCode.BACKEND_UNSUPPORTED
    assert words in " | ".join(exc.value.details["problems"])


@pytest.mark.parametrize("raw, words", [
    (_mirror_lens(ray_aiming="real"), "mirrors with real ray aiming"),
    (_mirror_lens(aperture={"type": "float_by_stop"}, ray_aiming="real",
                  surfaces=[{"radius": 40, "thickness": 5, "material": {"model": {"nd": 1.5, "vd": 60}}},
                            {"radius": "inf", "thickness": 40, "stop": True, "semi_diameter": 2}]), "float by stop size"),
])
def test_real_aiming_on_a_stop_defined_aperture_or_a_mirror_is_built_and_labelled(raw, words):
    backend = _built(raw)
    assert words in " | ".join(backend.unverified())
    assert math.isfinite(backend.operand("EFFL", 0, 0, []))


def test_a_mirror_cannot_be_added_or_removed_on_a_built_lens():
    backend = _built(_mirror_lens())
    with pytest.raises(SaosError) as exc:
        backend.set_param("1.material", "")
    assert exc.value.code == ErrorCode.BACKEND_UNSUPPORTED


def test_operands_rtc_does_not_compute_are_refused():
    backend = _built(_singlet())
    with pytest.raises(SaosError) as exc:
        backend.operand("MTFT", 0, 0, [])
    assert exc.value.code == ErrorCode.BACKEND_UNSUPPORTED
    backend.operand("EFFL", 0, 0, [])
    prov = backend.provenance()
    assert prov["backend"] == "rtc" and "EFFL" in prov["operands_unverified"]
    assert backend.provenance()["operands_unverified"] == []


def test_rtc_instance_refuses_operations_that_need_opticstudio():
    daemon = Daemon(RtcBackend(None, standalone=True), {"backend": "rtc"})
    for op in sorted(OPTICSTUDIO_ONLY):
        reply = daemon.handle({"op": op, "args": {}})
        assert reply["ok"] is False and reply["error"]["code"] == ErrorCode.BACKEND_UNSUPPORTED, op


def test_live_view_says_where_the_numbers_come_from():
    backend = _built(_singlet())
    view = backend.viz_data(None, full=False)
    assert view["backend"] == "rtc"
    assert view["unverified"] == backend.unverified()
    assert view["interference"]["crossings"] == [] and view["interference"]["overlaps"] == []
    # 布局光线的第一个点落在第 1 面上 / layout rays start on surface 1
    first = view["layout"]["rays"][0]["points"][0]
    assert first[2] == pytest.approx(backend.sag(1, first[0], first[1]))


def test_copy_mode_still_refuses_settings_it_cannot_check(tmp_path):
    backend = _built(_singlet())
    backend.save_as(tmp_path / "finite.zmx")
    with pytest.raises(SaosError) as exc:
        RtcBackend(tmp_path / "finite.zmx")
    assert exc.value.code == ErrorCode.BACKEND_UNSUPPORTED


def test_paraxial_conic_reads_as_opticstudio_does():
    """近轴面没有圆锥系数：ZOS-API 读作 inf、文件存 0。rtc 若读 0，声明为环境面的近轴源面在结构指纹里与 OpticStudio
    记下的批准记录不符，轮次门禁拒绝运行（fiber-var-zoom，2026-10-30）。"""
    from saos import model_gate

    backend = _built({"title": "paraxial source", "aperture": {"type": "entrance_pupil_diameter", "value": 1.0},
                      "fields": {"type": "angle", "y": [0]}, "wavelengths": {"um": [0.92]},
                      "object": {"thickness": "inf"},
                      "surfaces": [{"type": "paraxial", "focal_length": 4.0, "thickness": 4.0, "stop": True}]})
    assert math.isinf(backend.surfaces()[1]["conic"])
    assert math.isinf(backend.get_param("1.conic"))
    desc = backend.describe_model(None)
    assert math.isinf(desc["surfaces"][1]["conic"])
    assert model_gate.fingerprint(desc, [1])["surfaces"][1]["conic"] == model_gate._num(math.inf)
    # kept 恢复时把读出的 inf 写回，不改镜头；其他值无处可写
    backend.set_param("1.conic", math.inf)
    with pytest.raises(SaosError) as err:
        backend.set_param("1.conic", -1.0)
    assert err.value.code == ErrorCode.BAD_ARGS


@needs_glass
def test_mirror_material_reads_as_mirror_and_has_mirror_source():
    backend = RtcBackend(CASES / "reflective.zmx", standalone=True)
    assert backend.get_param("1.material") == "MIRROR"
    assert backend.surfaces()[1]["material"] == "MIRROR"
    assert backend.describe_model(None)["surfaces"][1]["material_source"] == {"kind": "mirror"}


@needs_glass
def test_float_stop_semi_variable_changes_pupil_without_changing_structure(tmp_path):
    from saos import model_gate
    from rtc import zmx

    backend = RtcBackend(CASES / "iris.zmx", standalone=True)
    before = model_gate.fingerprint(backend.describe_model(None))
    diameter = backend.operand("EPDI", 0, 0, [])
    backend.vary(6, "semi_diameter", True)
    backend.set_param("6.semi_diameter", 2.0)
    assert "6.semi_diameter" in backend.variables()
    assert backend.optical_system(backend.lens.primary_wave).items[5].semi == 2.0
    assert backend.operand("EPDI", 0, 0, []) == pytest.approx(diameter * 0.8)
    assert model_gate.fingerprint(backend.describe_model(None)) == before
    legacy = {**before, "aperture": {**before["aperture"], "value": diameter}}
    assert model_gate.fingerprint_changes(legacy, before) == []
    target = tmp_path / "iris.zmx"
    backend.save_as(target)
    assert zmx.parse(target).surface(6).semi_diameter == 2.0
    assert backend.get_param("6.semi_diameter") == 2.0
    backend.load(target)
    assert "6.semi_diameter" not in backend.variables()
    assert backend.get_param("6.semi_diameter") == 2.0


def test_floating_aperture_dump_tracks_the_changed_semi_diameter():
    from case_variants import BY_NAME
    from rtc import zmx
    from saos.backend import rtc_lens

    lens = BY_NAME["iris"].lens()
    stop = lens.surface(6)
    stop.aperture_code, stop.ap_max = 10, stop.semi_diameter
    stop.semi_diameter = 1.8
    loaded = zmx.parse_text(rtc_lens.dump(lens)).surface(6)
    assert loaded.semi_diameter == loaded.ap_max == 1.8


@needs_glass
def test_description_keeps_recorded_field_vignetting(tmp_path):
    from case_variants import BY_NAME
    from saos.backend import rtc_lens

    lens = BY_NAME["iris_vignetting"].lens()
    target = tmp_path / "iris.zmx"
    rtc_lens.write(lens, target)
    backend = RtcBackend(target, standalone=True)
    fields = backend.describe_model(None)["fields"]["items"]
    for field, values in zip(fields, lens.vignetting):
        assert [field["vignetting"][key] for key in ("VDX", "VDY", "VCX", "VCY", "VAN")] == list(values)


@needs_glass
def test_obscuration_view_keeps_the_mirror_outer_edge():
    from saos.report_sheet import lens_svg

    backend = RtcBackend(CASES / "reflective.zmx", standalone=True)
    view = backend.viz_data(None, full=False)
    mirror = next(s for s in view["surfaces"] if s["index"] == 1)
    assert max(abs(p[1]) for p in mirror["profile_y"]) == pytest.approx(45.0)
    mask = mirror["obscuration"]["profile_y"]
    assert max(abs(p[1]) for line in mask for p in line) == pytest.approx(15.0)
    assert "stroke-dasharray" in lens_svg(view)
