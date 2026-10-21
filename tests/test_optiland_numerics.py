"""真实 Optiland 的一阶数值检查（不用假后端）。容差在运行前按来源登记：
Real-Optiland first-order checks (no fake backend). Tolerances are registered before the run, with their basis:

- 参考值用薄/厚透镜的解析式（近轴，单位 mm）；N-BK7 在 587.6 nm 的 nd 取 1.5168（标称值，精度 ±1e-4）。
  折射率不确定 1e-4 时焦距变化约 0.01 mm，所以焦距容差取 0.02 mm（约 4e-4 相对）。
- 总长 TOTR 由处方直接给出（5 mm + 95 mm），容差 1e-9 mm，只防止单位或求和错误。
"""

from __future__ import annotations

import pytest

from saos import prescription
from saos.backend.optiland import OptilandBackend
from test_lens_snapshot_files import PRESCRIPTION

N_BK7_ND = 1.5168
FOCAL_TOLERANCE_MM = 0.02
TOTAL_TRACK_TOLERANCE_MM = 1e-9


def thick_lens_focal_length(n: float, r1: float, r2: float, d: float) -> float:
    """薄厚透镜的近轴焦距（同一介质两侧为空气）。 / Paraxial focal length of a thick lens in air."""
    return 1.0 / ((n - 1.0) * (1.0 / r1 - 1.0 / r2 + (n - 1.0) * d / (n * r1 * r2)))


@pytest.fixture(scope="module")
def singlet():
    backend = OptilandBackend()
    backend.start()
    backend.build(prescription.normalize(PRESCRIPTION))
    yield backend
    backend.close()


def test_first_order_focal_length_matches_the_thick_lens_formula(singlet):
    expected = thick_lens_focal_length(N_BK7_ND, 50.0, -50.0, 5.0)
    measured = singlet.operand("EFFL", 0, 0, [])
    assert abs(measured - expected) <= FOCAL_TOLERANCE_MM, (measured, expected)


def test_total_track_is_the_sum_of_the_prescribed_thicknesses(singlet):
    assert abs(singlet.operand("TOTR", 0, 0, []) - (5.0 + 95.0)) <= TOTAL_TRACK_TOLERANCE_MM


def test_an_object_at_infinity_builds_without_a_runtime_warning():
    """A1：无穷远物体的 z 平移为 -∞，建模时曾因 0·∞ 报 invalid value in matmul。修复前此处即失败。
    A1: an object at infinity carries z = -inf; building used to warn 'invalid value in matmul' (0·inf). Fails before the fix."""
    import warnings

    backend = OptilandBackend()
    backend.start()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            backend.build(prescription.normalize(PRESCRIPTION))
        assert backend.optic.surfaces[0].geometry.cs.z == float("-inf")
        assert backend.operand("EFFL", 0, 0, []) == pytest.approx(thick_lens_focal_length(N_BK7_ND, 50.0, -50.0, 5.0), abs=FOCAL_TOLERANCE_MM)
    finally:
        backend.close()


def test_rotation_keeps_an_infinite_translation_on_the_rotated_axis():
    """_rotate 跳过零元素：绕 x 轴转 90° 后，沿 z 的无穷平移应落到 -y 方向，其余分量仍为 0（不是 NaN）。
    Rotating an infinite z translation by 90° about x must give -inf along y and exact zeros elsewhere (not NaN)."""
    import numpy as np

    from saos.backend.optiland_surfaces import _rotate

    quarter_turn_about_x = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]])
    out = _rotate(quarter_turn_about_x, np.array([0.0, 0.0, float("inf")]))
    assert out[0] == 0.0 and out[1] == float("-inf") and out[2] == 0.0
    assert np.array_equal(_rotate(np.eye(3), np.array([1.5, -2.0, 3.0])), np.array([1.5, -2.0, 3.0]))
