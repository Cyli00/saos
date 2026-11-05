"""材料描述与环境；波长为 µm、温度为 °C、压力为 atm。

错误分两类：MaterialInvalid 是调用方给的数值或名称本身无效（脚本参数错误）；
MaterialUnsupported 是输入合法、但 rtc 还不能按 OpticStudio 复现（能力缺口或未准入）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from numbers import Real


class MaterialUnsupported(NotImplementedError):
    """材料数据或计算路径不能由当前材料层复现。"""


class MaterialInvalid(ValueError):
    """材料描述、环境或波长本身无效。"""


def _finite(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, Real) and math.isfinite(value)


@dataclass(frozen=True)
class Environment:
    temperature: float = 20.0
    pressure: float = 1.0
    adjust: bool = False

    def __post_init__(self):
        if not _finite(self.temperature) or self.temperature <= -273.15:
            raise MaterialInvalid(f"环境温度须为高于绝对零度的有限数：{self.temperature}")
        if not _finite(self.pressure) or self.pressure < 0:
            raise MaterialInvalid(f"环境压力须为非负有限数：{self.pressure}")
        if not isinstance(self.adjust, bool):
            raise MaterialInvalid("Adjust Index 须为布尔值")


@dataclass(frozen=True)
class Catalog:
    name: str

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip():
            raise MaterialInvalid("目录材料须有非空名称")
        object.__setattr__(self, "name", self.name.strip().upper())


@dataclass(frozen=True)
class Model:
    nd: float
    vd: float
    dpgf: float = 0.0

    def __post_init__(self):
        if not all(_finite(v) for v in (self.nd, self.vd, self.dpgf)) or self.nd <= 0:
            raise MaterialInvalid("模型玻璃须有有限 nd/Vd/dPgF，且 nd > 0")
        # 统一成 float：同值的描述相等，可作缓存键
        for name in ("nd", "vd", "dpgf"):
            object.__setattr__(self, name, float(getattr(self, name)))


@dataclass(frozen=True)
class Mirror:
    """反射面（``.zmx`` 的材料列写 ``MIRROR``）。

    序列模式约定：反射面之后介质不变，内部用带符号的折射率记录（见 ``rtc.system``）；
    膜层的反射率不进追迹（OpticStudio 未镀膜的反射面按铝 0.7−7.0i 计，rtc 只算几何）。
    """

    name: str = "MIRROR"


MaterialSpec = Catalog | Model | Mirror | None


def material_spec(value) -> MaterialSpec:
    """脚本可交换表示：目录名、空气空串、MIRROR，或 nd/vd/dpgf 字典。"""
    if value is None or isinstance(value, (Catalog, Model, Mirror)):
        return value
    if isinstance(value, str):
        if value.strip().upper() == "MIRROR":
            return Mirror()
        return Catalog(value) if value.strip() else None
    if isinstance(value, dict) and {"nd", "vd"} <= value.keys() and not value.keys() - {"nd", "vd", "dpgf"}:
        return Model(value["nd"], value["vd"], value.get("dpgf", 0.0))
    raise MaterialInvalid("材料须为目录名、空气空串、MIRROR，或含 nd/vd 和可选 dpgf 的有限数值字典")
