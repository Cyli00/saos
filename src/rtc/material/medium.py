"""面后介质的统一相对折射率接口；材料知识集中在此处。"""

from dataclasses import dataclass

import numpy as np

from .agf import Glass
from .air import n_air
from .formulas import FORMULAS, wavelengths
from .model import model_index
from .thermal import delta_absolute_index
from .types import Catalog, Environment, MaterialInvalid, MaterialSpec, MaterialUnsupported, Mirror, Model


class Medium:
    reflect = False

    def index(self, wavelength_um, environment: Environment):
        if not isinstance(environment, Environment):
            raise MaterialInvalid("材料计算须显式提供 Environment")
        w = wavelengths(wavelength_um)
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            result = self._index(w, environment)
        if not np.all(np.isfinite(result) & (result > 0)):
            raise MaterialUnsupported("所给材料及环境产生非有限或非正折射率")
        return result

    def path(self, environment: Environment) -> str:
        """计算路径的准入键（见 rtc.material.admission）：同一键下的算术完全相同。"""
        raise NotImplementedError


class Air(Medium):
    def _index(self, w, env):
        return np.ones_like(w)

    def path(self, environment):
        return "air"


class MirrorMedium(Medium):
    """反射面：光不穿过它，折射率取入射侧的值；带符号的记账在 ``rtc.system`` 里做。"""

    reflect = True

    def _index(self, w, env):
        return np.ones_like(w)

    def path(self, environment):
        return "mirror"


@dataclass(frozen=True)
class CatalogGlass(Medium):
    glass: Glass

    def _index(self, w, env):
        g = self.glass
        lo, hi = g.wavelength_range
        if not np.isfinite(lo) or not np.isfinite(hi) or lo <= 0 or hi < lo:
            raise MaterialUnsupported(f"{g.name}: LD 范围无效或缺失")
        if np.any((w < lo * 0.999) | (w * 0.999 > hi)):
            raise MaterialUnsupported(f"{g.name}: 波长超出 LD {lo}–{hi} µm（含 0.999 容差）")
        if not env.adjust:
            return g.index(w)
        if g.tref is None or not np.isfinite(g.tref):
            raise MaterialUnsupported(f"{g.name}: 参考温度无效")
        na = n_air(w, env.temperature, env.pressure)
        # 参考空气使用原始系统波长；改为 wr 会在已有样本中超过 1e-8。
        nr = n_air(w, g.tref, 1.0)
        wr = w * na / nr
        n = g.index(wr)
        dn = delta_absolute_index(n, wr, env.temperature - g.tref, g.thermal)
        return (n * nr + dn) / na

    def path(self, environment):
        g = self.glass
        if not environment.adjust or (environment.temperature == g.tref and environment.pressure == 1.0):
            mode = "fixed"
        elif environment.temperature == g.tref:
            mode = "pressure"
        else:
            mode = "thermal"
        return f"catalog:{g.formula}:{mode}"


@dataclass(frozen=True)
class ModelGlass(Medium):
    spec: Model

    def _index(self, w, env):
        n = model_index(w, self.spec.nd, self.spec.vd, self.spec.dpgf)
        if env.adjust:
            n = n_air(w, env.temperature, 1.0) / n_air(w, env.temperature, env.pressure) * n
        return n

    def path(self, environment):
        mode = "fixed" if not environment.adjust or environment.pressure == 1.0 else "pressure"
        # dPgF ≠ 0 单独成键：已有应用级证据只覆盖 dPgF = 0
        return f"model:{mode}" + (":dpgf" if self.spec.dpgf != 0.0 else "")


def resolve(spec: MaterialSpec, catalog=None, catalogs=None) -> Medium:
    if spec is None:
        return Air()
    if isinstance(spec, Mirror):
        return MirrorMedium()
    if isinstance(spec, Model):
        return ModelGlass(spec)
    if not isinstance(spec, Catalog):
        raise MaterialInvalid(f"不支持的材料描述：{type(spec).__name__}")
    if spec.name == "MIRROR":
        return MirrorMedium()
    if spec.name == "ABSORB" or spec.name.endswith(".ZTG"):
        raise MaterialUnsupported(f"{spec.name}: 吸收或表格介质尚未接入序列追迹")
    if catalog is None:
        raise MaterialUnsupported(f"{spec.name}: 未提供玻璃目录")
    try:
        glass = catalog.get(spec.name, catalogs)
    except KeyError as exc:
        raise MaterialInvalid(f"{spec.name}: 在用目录里没有这个牌号（{catalogs}）") from exc
    except OSError as exc:
        raise MaterialUnsupported(f"{spec.name}: 无法读取玻璃目录：{exc}") from exc
    if glass.data_error is not None:
        raise MaterialUnsupported(f"{spec.name}: 目录数据无效：{glass.data_error}")
    if glass.formula not in FORMULAS:
        raise MaterialUnsupported(f"{spec.name}: 色散公式 {glass.formula} 未实现")
    if glass.reference_error is not None:
        raise MaterialUnsupported(f"{spec.name}: 目录参考线计算失败：{glass.reference_error}")
    if glass.negative_index:
        raise MaterialUnsupported(f"{spec.name}: ED 负折射率材料尚未接入追迹符号约定")
    return CatalogGlass(glass)


def as_model(spec: MaterialSpec, catalog=None, catalogs=None) -> Model:
    """目录转模型沿用 saos 的约定：nd/Vd 取目录计算值，dPgF 取零。"""
    medium = resolve(spec, catalog, catalogs)
    if isinstance(medium, ModelGlass):
        return medium.spec
    if isinstance(medium, CatalogGlass):
        g = medium.glass
        if g.vd == 0.0:
            # LD 不覆盖 F–C 线时加载器把 nd/Vd 记为 1/0（红外材料常见），没有可扰动的可见光常数
            raise MaterialUnsupported(f"{g.name}: LD {g.wavelength_range} 不覆盖 F–C 线，没有可转成模型玻璃的 nd/Vd")
        return Model(g.nd, g.vd, 0.0)
    raise MaterialInvalid("空气没有可读取或扰动的 nd/Vd/dPgF")
