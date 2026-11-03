"""材料层：描述、环境、目录与介质求值；计算结果仅供 rtc 探索。"""

from .agf import Glass, GlassCatalog, catalog_dir, parse_agf
from .medium import Air, CatalogGlass, Medium, ModelGlass, as_model, resolve
from .types import Catalog, Environment, MaterialInvalid, MaterialSpec, MaterialUnsupported, Model, material_spec

__all__ = ["Environment", "MaterialSpec", "Catalog", "Model", "Medium", "Air", "CatalogGlass", "ModelGlass",
           "Glass", "GlassCatalog", "catalog_dir", "parse_agf", "resolve", "as_model", "material_spec",
           "MaterialUnsupported", "MaterialInvalid"]
