"""旧材料 API 的兼容入口；实现统一位于 rtc.material。"""

from .material.agf import Glass, GlassCatalog as Catalog, catalog_dir

__all__ = ["Glass", "Catalog", "catalog_dir"]
