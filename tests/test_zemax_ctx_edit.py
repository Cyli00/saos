"""SAOS_RUN_ZEMAX=1 时在真实 OpticStudio 的简单镜头上验证脚本编辑接口：整批评价函数行的顺序、引用与回读、拒绝后的恢复，
sag_gap 随厚度变化，以及求解与插面。只用新建的两面单透镜，不碰任务镜头。"""

from __future__ import annotations

import math
import os
import time

import pytest

from saos.backend.zos import ZosBackend, _load_zosapi
from saos.ctx import rows
from saos.protocol import SaosError

pytestmark = [pytest.mark.zemax, pytest.mark.skipif(os.environ.get("SAOS_RUN_ZEMAX") != "1", reason="需要显式启用真实 OpticStudio 回归")]


@pytest.fixture(scope="module")
def zos():
    backend = ZosBackend()
    try:
        backend.start()
        yield backend
    finally:
        backend.close()


@pytest.fixture
def singlet(zos):
    zos.new_system()
    lde = zos.system.LDE
    front = lde.GetSurfaceAt(1)
    front.Radius, front.Thickness = 50.0, 5.0
    zos.set_surface(1, "material", "N-BK7")
    back = lde.InsertNewSurfaceAt(2)
    back.Radius, back.Thickness = -50.0, 45.0
    zos.system.SystemData.Aperture.ApertureValue = 10.0
    zos.system.MFE.DeleteAllRows()
    return zos


def operand_values(zos):
    mfe = zos.system.MFE
    mfe.CalculateMeritFunction()
    values = [(str(mfe.GetOperandAt(i).Type), float(mfe.GetOperandAt(i).Value)) for i in range(1, mfe.NumberOfOperands + 1)]
    # 空行 BLNK 的值是 NaN，换成 None 才能比较 / an empty BLNK row has the value NaN; None compares
    return [(t, None if math.isnan(v) else v) for t, v in values]


def test_batch_keeps_order_before_dmfs_and_resolves_references(singlet):
    zos = singlet
    zos.merit_wizard({"rings": 3, "arms": 6})
    dmfs = [t for t, _ in operand_values(zos)].index("DMFS") + 1
    added = zos.merit_append([{"id": "efl", "type": "EFFL", "params": [0, 1], "target": 50, "weight": 1},
                              {"type": "OPGT", "params": [{"ref": "efl"}], "target": 45, "weight": 10}])
    assert added == [dmfs, dmfs + 1]
    values = operand_values(zos)
    assert values[dmfs - 1][0] == "EFFL" and values[dmfs][0] == "OPGT" and values[dmfs + 1][0] == "DMFS"
    row = zos.system.MFE.GetOperandAt(dmfs + 1)
    assert int(row.GetOperandCell(_load_zosapi(zos.root).Editors.MFE.MeritColumn.Param1).IntegerValue) == dmfs
    # 向导从 DMFS 起重建后，DMFS 之前的两行仍在 / rebuilding from DMFS keeps the rows above it
    zos.merit_wizard({"rings": 3, "arms": 6, "start_at": dmfs + 2})
    assert [t for t, _ in operand_values(zos)][dmfs - 1:dmfs + 2] == ["EFFL", "OPGT", "DMFS"]


def test_a_value_opticstudio_would_change_refuses_the_whole_batch(singlet):
    zos = singlet
    zos.merit_append([{"type": "TTHI", "params": [1, 1]}])
    before = operand_values(zos)
    # 系统只有 1 个波长：EFFL 的波长号 2 会被 OpticStudio 改成 1 / one wavelength: OpticStudio turns wave 2 into 1
    with pytest.raises(SaosError) as err:
        zos.merit_append([{"type": "TTHI", "params": [1, 2]}, {"type": "EFFL", "params": [0, 2]}])
    assert err.value.code == "BAD_ARGS" and "EFFL" in err.value.message
    assert operand_values(zos) == before


def test_sag_gap_follows_a_changing_thickness(singlet):
    zos = singlet
    added = zos.merit_append(rows.sag_gap(1, 2, [(0.0, 0.0), (4.0, 0.0)], minimum=1.0))
    lde = zos.system.LDE

    def gaps():
        values = dict((i, v) for i, (t, v) in enumerate(operand_values(zos), start=1))
        return [values[r] for r in added if str(zos.system.MFE.GetOperandAt(r).Type) == "SUMM"]

    def expected(x):
        sf, sb = zos.sag(1, x, 0.0), zos.sag(2, x, 0.0)
        return float(lde.GetSurfaceAt(1).Thickness) + sb - sf

    assert gaps() == pytest.approx([expected(0.0), expected(4.0)], abs=1e-12)
    lde.GetSurfaceAt(1).Thickness = 3.0
    assert gaps() == pytest.approx([expected(0.0), expected(4.0)], abs=1e-12)
    assert gaps()[0] == pytest.approx(3.0, abs=1e-12)


def test_solves_and_surfaces(singlet):
    zos = singlet
    assert zos.set_solve(2, "thickness", {"type": "marginal_ray_height", "height": 0.0, "zone": 0.0}) == {"type": "MarginalRayHeight"}
    assert zos.set_solve(2, "thickness", {"type": "fixed"}) == {"type": "Fixed"}
    assert zos.set_solve(1, "radius", {"type": "variable"}) == {"type": "Variable"}
    assert zos.insert_surface(2, "EvenAspheric") == "EvenAspheric"
    assert int(zos.system.LDE.NumberOfSurfaces) == 5
    assert zos.change_surface_type(2, "standard") == "Standard"
    with pytest.raises(SaosError):
        zos.change_surface_type(0, "Standard")


def test_batch_is_much_faster_than_row_by_row(singlet, capsys):
    zos = singlet
    api = _load_zosapi(zos.root)
    specs = rows.sag_gap(1, 2, [(4.0 * k / 300, 0.0) for k in range(300)], minimum=1.0)
    t = time.perf_counter()
    added = zos.merit_append(specs)
    batch = time.perf_counter() - t
    merit_batch = float(zos.system.MFE.CalculateMeritFunction())
    mfe = zos.system.MFE
    mfe.DeleteAllRows()
    columns = [getattr(api.Editors.MFE.MeritColumn, f"Param{n}") for n in range(1, 9)]
    t = time.perf_counter()
    for i, spec in enumerate(rows.compile_rows(specs, 1)):
        row = mfe.GetOperandAt(1) if i == 0 else mfe.AddOperand()
        row.ChangeType(getattr(api.Editors.MFE.MeritOperandType, spec["type"]))
        for column, value in zip(columns, spec["params"]):
            cell = row.GetOperandCell(column)
            if str(cell.DataType) == "Integer":
                cell.IntegerValue = int(value)
            elif str(cell.DataType) == "Double":
                cell.DoubleValue = float(value)
        row.Target, row.Weight = spec["target"], spec["weight"]
    single = time.perf_counter() - t
    assert float(mfe.CalculateMeritFunction()) == merit_batch
    with capsys.disabled():
        print(f"\n{len(added)} rows: batch {batch * 1000:.1f} ms, row by row {single * 1000:.0f} ms ({single / batch:.0f}x)")
    assert batch * 20 < single


def test_deeplens_style_constraints_measure_what_they_claim(singlet):
    """每个生成函数的取值与独立算出的量对照：厚度用 CTVA/ETVA，畸变用实际与近轴主光线高度，CRA 与偏折角用追迹方向。"""
    zos = singlet
    zos.system.SystemData.Fields.AddField(0, 10.0, 1.0)
    lde = zos.system.LDE
    image = int(lde.NumberOfSurfaces) - 1

    def rows_values(specs):
        zos.system.MFE.DeleteAllRows()
        added = zos.merit_append(specs)
        mfe = zos.system.MFE
        mfe.CalculateMeritFunction()
        return [(str(mfe.GetOperandAt(r).Type), float(mfe.GetOperandAt(r).Value)) for r in added]

    # 中心厚度 5、边缘 4.45：满足的界限取目标值，越界的取实际值 / a met bound reads its target, a violated one the actual value
    got = rows_values(rows.clearances(1, 1, glass_center=(6, 8), glass_edge=(1, None)))
    assert got == [("MNCG", pytest.approx(5.0)), ("MXCG", pytest.approx(8.0)), ("MNEG", pytest.approx(1.0))]
    assert rows_values(rows.clearances(1, 1, glass_edge=(10, None)))[0][1] == pytest.approx(zos.operand("ETVA", 1, 0, []), abs=1e-12)

    assert rows_values(rows.total_track(maximum=60))[0][1] == pytest.approx(sum(float(lde.GetSurfaceAt(i).Thickness) for i in range(1, image)))

    got = rows_values(rows.distortion(1, [0.5, 1.0], maximum=1.0))
    for hy, (_, value) in zip([0.5, 1.0], [g for g in got if g[0] == "DISG"]):
        real, paraxial = zos.operand("REAY", image, 1, [0, hy, 0, 0]), zos.operand("PARY", image, 1, [0, hy, 0, 0])
        assert value == pytest.approx(100 * (real - paraxial) / paraxial, abs=1e-9)

    def angle(a, b):
        return math.degrees(math.acos(max(-1.0, min(1.0, a[0] * b[0] + a[1] * b[1] + a[2] * b[2]))))

    def direction(rays, i):
        return (float(rays.l[i]), float(rays.m[i]), float(rays.n[i]))

    got = rows_values(rows.chief_ray_angle(image, 1, [0.5, 1.0], maximum=30))
    traced = zos.trace_batch([(1, 0, 0.5, 0, 0), (1, 0, 1.0, 0, 0)], image)
    assert [v for t, v in got if t == "RAID"] == pytest.approx([angle(direction(traced, i), (0, 0, 1)) for i in range(2)], abs=1e-9)

    rays_in = [(0.0, 1.0, 0.0, 1.0), (0.0, 1.0, 0.0, -1.0), (0.0, 0.0, 0.0, 1.0)]
    got = rows_values(rows.ray_bend([1, 2], 1, rays_in, maximum=30))
    bends = [v for t, v in got if t == "ABSO"]
    expected = []
    for hx, hy, px, py in rays_in:
        path = [zos.trace_batch([(1, hx, hy, px, py)], s) for s in (0, 1, 2)]
        expected += [angle(direction(path[0], 0), direction(path[1], 0)), angle(direction(path[1], 0), direction(path[2], 0))]
    assert bends == pytest.approx(expected, abs=1e-6)


def test_a_value_in_a_column_the_operand_does_not_use_is_refused(singlet):
    with pytest.raises(SaosError) as err:
        singlet.merit_append([{"type": "TOTR", "params": [3]}])
    assert "TOTR has no int1" in err.value.message
