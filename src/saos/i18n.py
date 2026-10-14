"""给人看的输出（交付报告、计划页、后台日志）的语言选择。给 agent 读的错误和提示只用英文，不经过这里。

优先级：显式指定（命令行 --lang、需求框定的 lang）> 环境变量 SAOS_LANG > 系统界面语言 > en。
"""

from __future__ import annotations

import locale
import os
import sys
from functools import cache

LANGS = ("en", "zh")
DEFAULT = "en"


def normalize(value: str | None) -> str | None:
    """'zh-CN'、'zh_TW'、'Chinese (Simplified)_China' 之类都归为 zh；认不出时返回 None。"""
    if not value:
        return None
    v = value.strip().lower()
    if v.startswith(("zh", "chinese")):
        return "zh"
    if v.startswith(("en", "english")):
        return "en"
    return None


@cache
def system_lang() -> str | None:
    if sys.platform == "win32":
        try:
            import ctypes

            # LANGID 低 10 位是主语言，0x04 为中文
            return "zh" if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x04 else "en"
        except (AttributeError, OSError):
            pass
    for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        if lang := normalize(os.environ.get(key)):
            return lang
    return normalize(locale.getlocale()[0])


def resolve(*explicit: str | None) -> str:
    for value in (*explicit, os.environ.get("SAOS_LANG")):
        if lang := normalize(value):
            return lang
    return system_lang() or DEFAULT


def text(lang: str, zh: str, en: str) -> str:
    return zh if lang == "zh" else en
