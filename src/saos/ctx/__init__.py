"""脚本编辑的辅助：rows 生成评价函数行（同批行用 id/ref 引用），hints 找出 ctx.get/set 已能替代的直接 ZOS-API 访问。
专家类在 saos.ctx.experts（L2）；本包是 L0，后端编译评价函数行时导入 rows，不能顺带加载专家类。
The experts live in saos.ctx.experts (L2); this package is L0 and backends import rows, so it must not load them."""

from . import rows

__all__ = ["rows"]
