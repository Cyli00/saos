"""文本解码：OpticStudio 的 .zmx / AGF 有两种编码（UTF-16LE+BOM、ASCII/UTF-8）。

必须先看 BOM：ASCII 文件按 utf-16 解也会"成功"，只是解出乱码（0x4343 → '\u4343'），
所以"能解出来"不等于"解对了"。
"""

from __future__ import annotations

BOM_UTF16_LE = b"\xff\xfe"
BOM_UTF16_BE = b"\xfe\xff"
BOM_UTF8 = b"\xef\xbb\xbf"


def decode_text(data: bytes) -> str:
    if data.startswith(BOM_UTF16_LE):
        return data.decode("utf-16-le").lstrip("\ufeff")
    if data.startswith(BOM_UTF16_BE):
        return data.decode("utf-16-be").lstrip("\ufeff")
    if data.startswith(BOM_UTF8):
        return data.decode("utf-8-sig")
    for enc in ("utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法解码文本")
