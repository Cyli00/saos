"""OpticStudio 数值 setter 把值舍成 15 位有效数字（2026-11-02 真机实测），exact_double 经 17 位字符串补齐末位；
有求解、求解类型被改或字符串解析不对时退回数值写入。这里用模拟单元格复现真机观察到的行为；真机回归在 test_zemax_kit 的 test_parameter_writes_and_kept_restore_every_bit。"""
from __future__ import annotations

from types import SimpleNamespace

from saos.backend.zos import exact_double

X = -0.020209160377426934          # 原例：数值 setter 读回 -0.0202091603774269


class Cell:
    """数值 setter 按 15 位舍入、Value 按字符串解析的单元格。parse 可换成错误的区域解析；drops_variable 模拟写字符串去掉变量状态。"""

    def __init__(self, solve="Fixed", parse=float, drops_variable=False):
        self.solve, self.parse, self.drops_variable, self.value, self.texts = solve, parse, drops_variable, 0.0, []

    def GetSolveData(self):
        return SimpleNamespace(Type=self.solve)

    @property
    def DoubleValue(self):
        return self.value

    @DoubleValue.setter
    def DoubleValue(self, v):
        self.value = float(f"{v:.15g}")

    @property
    def Value(self):
        return repr(self.value)

    @Value.setter
    def Value(self, text):
        self.texts.append(text)
        self.value = self.parse(text)
        if self.drops_variable and self.solve == "Variable":
            self.solve = "Fixed"

    def MakeSolveVariable(self):
        self.solve = "Variable"

    def MakeSolveFixed(self):
        self.solve = "Fixed"


def _write(cell, v):
    cell.DoubleValue = v
    return exact_double(cell, v, cell.DoubleValue)


def test_a_rounded_write_is_completed_through_the_string_and_an_exact_one_is_left_alone():
    cell = Cell()
    assert _write(cell, X) == X and cell.value == X
    plain = Cell()
    assert _write(plain, 0.25) == 0.25 and plain.texts == []
    assert _write(Cell(), float("inf")) == float("inf")
    variable = Cell(solve="Variable")
    assert _write(variable, X) == X and variable.solve == "Variable"


def test_a_cell_under_a_solve_keeps_the_numeric_write():
    cell = Cell(solve="Pickup")
    assert _write(cell, X) == float(f"{X:.15g}") and cell.texts == []
    assert exact_double(None, X, float(f"{X:.15g}")) == float(f"{X:.15g}")


def test_a_wrong_parse_or_a_lost_variable_status_falls_back_to_the_numeric_write():
    # 逗号小数的区域设置把 "." 当千分位：读回不对，退回数值写入而不留下错值
    comma = Cell(parse=lambda text: float(text.replace(".", "")))
    assert _write(comma, X) == float(f"{X:.15g}") == comma.value
    lost = Cell(solve="Variable", drops_variable=True)
    assert _write(lost, X) == float(f"{X:.15g}") and lost.solve == "Variable"


def test_minus_infinity_read_back_as_plus_infinity_did_not_take():
    from saos.backend.zos_params import _same

    inf = float("inf")
    assert not _same(inf, -inf) and not _same(-inf, inf) and not _same(1.0, inf)
    assert _same(inf, inf) and _same(1.0 + 1e-13, 1.0) and not _same(1.0 + 1e-11, 1.0)
