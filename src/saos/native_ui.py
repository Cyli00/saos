"""检查并准备 Interactive Extension，按原生控件连接指定窗口（extension 模式启动时调用）。"""

from __future__ import annotations

import ctypes
import os
import time
from pathlib import Path
from typing import Any

from saos import config
from saos.protocol import ErrorCode, SaosError

DIALOG_TITLE = "Interactive Extension"
OPEN_TIMEOUT_S = 10.0


def _main_title_suffix() -> str:
    major, minor, sp = config.EXPECTED_ZOS_VERSION
    return f" - Ansys Zemax OpticStudio 20{major} R{minor}.{sp:02d}"


def _waiting(item: dict[str, Any]) -> bool:
    return "waiting" in item.get("status", "").lower()


def require_ready(dialogs: list[dict[str, Any]]) -> dict[str, Any]:
    if len(dialogs) != 1:
        raise SaosError(ErrorCode.ZOS_ERROR, "需要且只能有一个等待连接的 Interactive Extension 对话框", {"dialogs": dialogs})
    item = dialogs[0]
    if item.get("auto_close") != "Off":
        raise SaosError(ErrorCode.ZOS_ERROR, "请取消 Auto Close on Disconnect 后再连接", item)
    if not _waiting(item) or not isinstance(item.get("instance"), int):
        raise SaosError(ErrorCode.ZOS_ERROR, "Interactive Extension 未处于可识别的等待连接状态", item)
    return item


def choose_window(windows: list[dict[str, Any]], pid: int | None = None) -> dict[str, Any]:
    candidates = [w for w in windows if pid is None or w["pid"] == pid]
    if len(candidates) != 1:
        raise SaosError(
            ErrorCode.ZOS_ERROR,
            "无法唯一确定显示窗口；可用 SAOS_UI_PID 指定 OpticStudio 进程",
            {"windows": windows, "requested_pid": pid},
        )
    return candidates[0]


def _top_windows(match) -> list[dict[str, Any]]:
    """可见与否都枚举顶层窗口，返回标题满足 match 的 {pid, hwnd, title}。"""
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    user.IsWindowVisible.argtypes = [ctypes.c_void_p]
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def visit(hwnd, _):
        title = ctypes.create_unicode_buffer(512)
        user.GetWindowTextW(hwnd, title, 512)
        if match(title.value, bool(user.IsWindowVisible(hwnd))):
            pid = ctypes.c_ulong()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.append({"pid": pid.value, "hwnd": int(hwnd), "title": title.value})
        return True

    user.EnumWindows(visit, 0)
    return found


def _uia():
    """加载 .NET Framework 的 UI Automation 程序集，返回 System.Windows.Automation 命名空间。"""
    import clr

    folder = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/WPF"
    clr.AddReference(str(folder / "UIAutomationTypes.dll"))
    clr.AddReference(str(folder / "UIAutomationClient.dll"))
    import System.Windows.Automation as automation

    return automation


def _element(hwnd: int):
    from System import IntPtr

    return _uia().AutomationElement.FromHandle(IntPtr(hwnd))


def discover_dialogs() -> list[dict[str, Any]]:
    uia = _uia()
    dialogs = []
    for w in _top_windows(lambda title, _visible: title == DIALOG_TITLE):
        item = {"pid": w["pid"], "hwnd": w["hwnd"], "instance": None, "auto_close": None, "status": ""}
        try:
            names = []
            for child in _element(w["hwnd"]).FindAll(uia.TreeScope.Subtree, uia.Condition.TrueCondition):
                c = child.Current
                kind = str(c.ControlType.ProgrammaticName)
                if kind == "ControlType.Text":
                    names.append(str(c.Name))
                elif kind == "ControlType.CheckBox" and c.Name == "Auto Close on Disconnect":
                    item["auto_close"] = str(child.GetCurrentPattern(uia.TogglePattern.Pattern).Current.ToggleState)
            item["instance"] = next((int(n) for n in names if n.isdigit()), None)
            item["status"] = next((n for n in names if "waiting" in n.lower() or n.lower() == "connected"), "")
        except Exception as exc:  # noqa: BLE001 - UIA 控件可能在枚举期间关闭，失败时禁止连接
            item["read_error"] = str(exc)
        dialogs.append(item)
    return dialogs


def _control(root, name: str, kind):
    uia = _uia()
    condition = uia.PropertyCondition(uia.AutomationElement.NameProperty, name)
    items = [e for e in root.FindAll(uia.TreeScope.Descendants, condition) if e.Current.ControlType == kind]
    if len(items) != 1 or not items[0].Current.IsEnabled:
        raise SaosError(ErrorCode.ZOS_ERROR, f"无法操作原生控件 {name}，窗口可能被其他对话框阻塞")
    return items[0]


def _requested_pid() -> int | None:
    requested = os.environ.get("SAOS_UI_PID")
    try:
        return int(requested) if requested else None
    except ValueError as exc:
        raise SaosError(ErrorCode.BAD_ARGS, "SAOS_UI_PID 必须是进程编号") from exc


def ensure_extension() -> dict[str, Any]:
    """找到（必要时打开）等待连接的 Interactive Extension 对话框，关闭 Auto Close 后返回其状态。"""
    uia = _uia()
    pid = _requested_pid()
    dialogs = discover_dialogs()
    if dialogs:
        dialog = choose_window(dialogs, pid)
    else:
        suffix = _main_title_suffix()
        target = choose_window(_top_windows(lambda title, visible: visible and suffix in title), pid)
        root = _element(target["hwnd"])
        _control(root, "Programming", uia.ControlType.TabItem).GetCurrentPattern(uia.SelectionItemPattern.Pattern).Select()
        _control(root, DIALOG_TITLE, uia.ControlType.Button).GetCurrentPattern(uia.InvokePattern.Pattern).Invoke()
        deadline = time.monotonic() + OPEN_TIMEOUT_S
        while not (dialogs := [d for d in discover_dialogs() if d["pid"] == target["pid"]]):
            if time.monotonic() >= deadline:
                raise SaosError(ErrorCode.ZOS_ERROR, "打开 Interactive Extension 后未检测到对话框")
            time.sleep(0.2)
        dialog = choose_window(dialogs, target["pid"])

    # 先确认处于等待状态再切换复选框，不接管已连接到其他扩展的窗口
    if not _waiting(dialog):
        raise SaosError(ErrorCode.ZOS_ERROR, "该窗口已连接或状态未知，不接管其他扩展连接", dialog)
    if dialog["auto_close"] == "On":
        checkbox = _control(_element(dialog["hwnd"]), "Auto Close on Disconnect", uia.ControlType.CheckBox)
        checkbox.GetCurrentPattern(uia.TogglePattern.Pattern).Toggle()
    return require_ready([d for d in discover_dialogs() if d["pid"] == dialog["pid"]])


# ---- 截图 ----


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


def encode_png(width: int, height: int, bgra: bytes) -> bytes:
    """32 位 BGRA（自上而下）编码为 RGBA PNG。"""
    import struct
    import zlib

    rgba = bytearray(bgra)
    rgba[0::4], rgba[2::4] = bgra[2::4], bgra[0::4]
    stride = width * 4
    raw = b"".join(b"\x00" + bytes(rgba[y * stride : (y + 1) * stride]) for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def capture_window(hwnd: int) -> tuple[int, int, bytes] | None:
    """用 PrintWindow（PW_RENDERFULLCONTENT）截取窗口，窗口被遮挡时也能得到内容；最小化时返回 None。"""
    user = ctypes.WinDLL("user32", use_last_error=True)
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)

    class Rect(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    user.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(Rect)]
    user.GetWindowDC.argtypes = [ctypes.c_void_p]
    user.GetWindowDC.restype = ctypes.c_void_p
    user.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    user.IsIconic.argtypes = [ctypes.c_void_p]
    gdi.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    gdi.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi.SelectObject.restype = ctypes.c_void_p
    gdi.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    gdi.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi.DeleteDC.argtypes = [ctypes.c_void_p]

    if user.IsIconic(hwnd):
        return None
    rect = Rect()
    user.GetWindowRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    window_dc = user.GetWindowDC(hwnd)
    mem_dc = gdi.CreateCompatibleDC(window_dc)
    bitmap = gdi.CreateCompatibleBitmap(window_dc, width, height)
    old = gdi.SelectObject(mem_dc, bitmap)
    try:
        user.PrintWindow(hwnd, mem_dc, 2)
        info = _BitmapInfoHeader(ctypes.sizeof(_BitmapInfoHeader), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        buffer = ctypes.create_string_buffer(width * height * 4)
        lines = gdi.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(info), 0)
        if lines != height:
            return None
        return width, height, buffer.raw
    finally:
        gdi.SelectObject(mem_dc, old)
        gdi.DeleteObject(bitmap)
        gdi.DeleteDC(mem_dc)
        user.ReleaseDC(hwnd, window_dc)


def capture_windows(pid: int, folder: Path, prefix: str) -> list[str]:
    """截取该进程所有有标题的可见顶层窗口（主窗口和浮动的分析窗口），存为 prefix-N.png。"""
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    windows = [w for w in _top_windows(lambda title, visible: visible and bool(title.strip())) if w["pid"] == pid]
    for k, w in enumerate(windows, 1):
        shot = capture_window(w["hwnd"])
        if shot is None:
            continue
        path = folder / f"{prefix}-{k}.png"
        path.write_bytes(encode_png(*shot))
        saved.append(path.name)
    return saved
