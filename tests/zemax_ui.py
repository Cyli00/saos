"""真实界面测试的辅助：温和地关闭测试自己启动的 OpticStudio，不留下自动保存的恢复文件。

强制结束 OpenStudio 会在 Documents/Zemax/Autosave/Recovered 留下恢复文件，下次启动弹出
"Autosave Recovery Notification" 模态框，分析窗口一直停在 Working 0%。
"""

from __future__ import annotations

import ctypes
import subprocess
import time
from pathlib import Path

from saos import native_ui

RECOVERED = Path.home() / "Documents/Zemax/Autosave/Recovered"
WM_CLOSE = 0x0010
NO_BUTTONS = ("No", "&No", "否", "否(N)", "否(&N)", "Don't Save", "不保存")


def close_dialog(hwnd: int) -> None:
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    user.PostMessageW(hwnd, WM_CLOSE, None, None)


def _alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, errors="replace").stdout
    return str(pid) in out


def _press_no(hwnd: int) -> bool:
    uia = native_ui._uia()
    try:
        root = native_ui._element(hwnd)
        for element in root.FindAll(uia.TreeScope.Descendants, uia.Condition.TrueCondition):
            current = element.Current
            if current.ControlType == uia.ControlType.Button and str(current.Name) in NO_BUTTONS:
                element.GetCurrentPattern(uia.InvokePattern.Pattern).Invoke()
                return True
    except Exception:  # noqa: BLE001 - 对话框可能已经关闭
        pass
    return False


def close_opticstudio(pid: int, timeout: float = 60.0) -> bool:
    """先关提示框，再关主窗口，遇到保存提示选"否"。返回是否正常退出；超时则强制结束并清理恢复文件。"""
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    suffix = native_ui._main_title_suffix()
    deadline = time.monotonic() + timeout
    sent = False
    while time.monotonic() < deadline and _alive(pid):
        windows = [w for w in native_ui._top_windows(lambda t, v: v and bool(t.strip())) if w["pid"] == pid]
        for w in windows:
            title = w["title"]
            if title in ("Autosave Recovery Notification", "Zemax Error Message"):
                user.PostMessageW(w["hwnd"], WM_CLOSE, None, None)
            elif suffix not in title and _press_no(w["hwnd"]):
                pass
        if not sent:
            for w in windows:
                if suffix in w["title"]:
                    user.PostMessageW(w["hwnd"], WM_CLOSE, None, None)
                    sent = True
        time.sleep(1)
    if not _alive(pid):
        return True
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    return False


def clean_recovered(original_root: Path) -> list[str]:
    """删除原路径在 original_root 之下的恢复文件及其索引行；其他恢复记录不动。"""
    db = RECOVERED / "_RecoveredFiles.db"
    if not db.is_file():
        return []
    root = str(original_root.resolve()).lower()
    kept, removed = [], []
    for line in db.read_text(encoding="utf-8-sig").splitlines():
        name, _, original = line.partition(",")
        if original.lower().startswith(root):
            (RECOVERED / name).unlink(missing_ok=True)
            removed.append(name)
        elif line.strip():
            kept.append(line)
    if kept:
        db.write_text("﻿" + "\r\n".join(kept) + "\r\n", encoding="utf-8")
    else:
        db.unlink()
    return removed
