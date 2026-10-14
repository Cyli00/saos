from __future__ import annotations

import math

import pytest

from saos.protocol import SaosError
from saos.recipes.microscope.spec import berek_check, parse_spec, sample_points


def base(**overrides):
    data = {
        "spec_version": 1,
        "correction_class": "plan_achromat",
        "magnification": 10,
        "na": {"value": 0.25, "source": "assumption", "basis": "李 p196"},
        "limits": {"working_distance_min": 10.6},
    }
    data.update(overrides)
    return data


def test_berek_matches_iso_19012_1_annex_a():
    # ISO 19012-1:2013 附录 A 表值（µm）。三个表值都与 λ ≈ 0.5465 µm 吻合（附录文字写 0.55），
    # 本项目用 λe = 0.54607 µm；加上表值只印 3 位小数，差异不超过 0.05%
    values = berek_check()
    assert values["4x/0.1"] == pytest.approx(114.825, rel=5e-4)
    assert values["10x/0.25"] == pytest.approx(18.372, rel=5e-4)
    assert values["40x/0.65"] == pytest.approx(1.993, rel=5e-4)


def test_derived_values_for_10x_plan_achromat():
    spec = parse_spec(base())
    assert spec.objective_focal_length == 20
    assert spec.objective_field_number == 22
    assert spec.delta_ob_um == pytest.approx(4.369, abs=1e-3)  # ISO 19012-2 附录 A：4.37
    assert spec.berek_image_um == pytest.approx(1837.2, abs=0.5)
    assert spec.airy_radius_image_um == pytest.approx(13.32, abs=0.01)
    ids = [i.id for i in spec.items]
    assert {"plan_field", "field_rms_wfe", "lateral_color"} <= set(ids)
    assert "mtf" not in ids and "distortion" not in ids
    assert spec.item("axial_color").threshold == pytest.approx(2 * spec.delta_ob_um)


def test_achromat_defaults_have_no_field_targets():
    spec = parse_spec(base(correction_class="achromat"))
    ids = [i.id for i in spec.items]
    assert spec.objective_field_number == 18
    assert not {"plan_field", "field_rms_wfe", "lateral_color"} & set(ids)


def test_achromat_field_wfe_requires_range_and_keeps_astig():
    with pytest.raises(SaosError):
        parse_spec(base(correction_class="achromat", targets={"field_rms_wfe": {}}))
    spec = parse_spec(base(correction_class="achromat", targets={"field_rms_wfe": {"range": 0.6}}))
    item = spec.item("field_rms_wfe")
    assert item.params == {"range": 0.6, "remove_defocus_astig": False}
    assert 0.6 in spec.field_samples()


def test_camera_enables_mtf_at_pixel_nyquist():
    spec = parse_spec(base(receiver="camera", pixel_size=6.5))
    assert spec.item("mtf").params["frequency"] == pytest.approx(500 / 6.5)
    with pytest.raises(SaosError):
        parse_spec(base(receiver="camera"))


def test_zero_threshold_needs_positive_scale():
    with pytest.raises(SaosError):
        parse_spec(base(limits={"working_distance_min": 1, "min_air_gap": {"value": 0, "scale": 0}}))
    spec = parse_spec(base())
    gap = spec.item("air_gap")
    assert (gap.threshold, gap.scale) == (0.0, 0.01)
    assert gap.gap(-0.01) == pytest.approx(1.0)
    assert gap.gap(0.0) == 0.0


def test_rejects_out_of_scope():
    for bad in (base(na=0.8), base(correction_class="plan_apochromat"), base(spec_version=2)):
        with pytest.raises(SaosError):
            parse_spec(bad)
    with pytest.raises(SaosError):
        parse_spec(base(wavelengths={"um": [0.4861, 0.5876, 0.6563]}))


def test_sample_points_include_boundary():
    assert sample_points(0.6) == [0.0, 0.3, 0.5, 0.6]
    assert sample_points(1.0) == [0.0, 0.3, 0.5, 0.7, 0.85, 1.0]


def test_item_gap_kinds():
    spec = parse_spec(base())
    assert spec.item("magnification").gap(0.075) == pytest.approx(0.5)
    assert spec.item("working_distance").gap(5.3) == pytest.approx(0.5)
    assert spec.item("glass_status").gap(2.0) == 2.0
    assert math.isclose(spec.item("axial_rms_wfe").gap(1 / 7), 1.0)
