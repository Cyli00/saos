"""显微物镜配方的标准设计布局。

标准设计布局（lens build 生成，验收评估据此找出物镜部分）：

    面 0      物面（有盖玻片时介质为盖玻片，厚度为盖玻片厚度）       注释 saos:object
    面 1      盖玻片上表面，厚度为工作距离间隔（仅有盖玻片时）       注释 saos:cover_top
    ...       物镜各面（可含注释为 saos:stop 的虚拟光阑面）
    面 k      参考筒镜：Paraxial 面，焦距 f_TL，后接 f_TL 到像面      注释 saos:tube
    面 k+1    像面

没有盖玻片时，物面厚度就是工作距离间隔。
"""

from __future__ import annotations

from typing import Any

from saos.protocol import ErrorCode, SaosError

TAG_OBJECT = "saos:object"
TAG_COVER_TOP = "saos:cover_top"
TAG_STOP = "saos:stop"
TAG_TUBE = "saos:tube"


def objective_block(rows: list[dict[str, Any]]) -> tuple[int, int]:
    """在标准设计布局的面列表中找出物镜部分，返回 [first, last]（面序号，含两端）。"""
    comments = [str(r.get("comment", "")) for r in rows]
    if not comments or comments[0] != TAG_OBJECT:
        raise SaosError(ErrorCode.BAD_ARGS, "not a saos standard layout: the object surface comment is not saos:object")
    try:
        tube = comments.index(TAG_TUBE)
    except ValueError as exc:
        raise SaosError(ErrorCode.BAD_ARGS, "not a saos standard layout: no saos:tube reference tube lens") from exc
    first = 2 if len(comments) > 1 and comments[1] == TAG_COVER_TOP else 1
    if tube <= first:
        raise SaosError(ErrorCode.BAD_ARGS, "the objective part of the standard layout is empty")
    return first, tube - 1


def gap_surface(rows: list[dict[str, Any]]) -> int:
    """工作距离间隔所在的面（其厚度即物方调焦量）。"""
    return 1 if len(rows) > 1 and rows[1].get("comment") == TAG_COVER_TOP else 0
