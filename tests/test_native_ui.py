from __future__ import annotations

import pytest

from saos.native_ui import choose_window, require_ready
from saos.protocol import SaosError


@pytest.mark.parametrize(
    "dialogs",
    [
        [],
        [{"auto_close": "On", "status": "Waiting", "instance": 1}],
        [{"auto_close": "Off", "status": "Connected", "instance": 1}],
        [{"auto_close": "Off", "status": "Waiting", "instance": None}],
        [{"auto_close": "Off", "status": "Waiting", "instance": 1}] * 2,
    ],
)
def test_extension_preflight_fails_closed(dialogs):
    with pytest.raises(SaosError):
        require_ready(dialogs)


def test_extension_preflight_preserves_explicit_instance():
    item = {
        "pid": 55,
        "instance": 2,
        "auto_close": "Off",
        "status": "Waiting for connection...",
    }
    assert require_ready([item]) == item


def test_window_selection_requires_unambiguous_target():
    windows = [{"pid": 1, "hwnd": 11}, {"pid": 2, "hwnd": 22}]
    with pytest.raises(SaosError):
        choose_window(windows)
    with pytest.raises(SaosError):
        choose_window(windows, 3)
    assert choose_window(windows, 2) == windows[1]
    assert choose_window(windows[:1]) == windows[0]


def test_gdi_screenshot_png_is_opaque():
    import struct
    import zlib

    from saos.native_ui import encode_png

    png = encode_png(2, 1, bytes([30, 20, 10, 0, 60, 50, 40, 123]))
    offset = 8
    compressed = b""
    while offset < len(png):
        size = struct.unpack(">I", png[offset:offset + 4])[0]
        if png[offset + 4:offset + 8] == b"IDAT":
            compressed += png[offset + 8:offset + 8 + size]
        offset += size + 12
    assert zlib.decompress(compressed) == bytes([0, 10, 20, 30, 255, 40, 50, 60, 255])


def test_extension_readiness_never_raises(monkeypatch):
    import saos.native_ui as ui

    monkeypatch.delenv("SAOS_UI_PID", raising=False)
    monkeypatch.setattr(ui, "discover_dialogs", lambda: [])
    two = [{"pid": 1, "hwnd": 11, "title": "a"}, {"pid": 2, "hwnd": 22, "title": "b"}]
    assert ui.extension_readiness([])["ready"] is False
    assert ui.extension_readiness(two)["ready"] is False
    monkeypatch.setenv("SAOS_UI_PID", "2")
    assert ui.extension_readiness(two) == {"ready": True, "via": "window", "pid": 2}

    monkeypatch.setattr(ui, "discover_dialogs", lambda: [{"pid": 2, "status": "Connected"}])
    assert ui.extension_readiness(two)["ready"] is False
    monkeypatch.setattr(ui, "discover_dialogs", lambda: [{"pid": 2, "status": "Waiting for connection..."}])
    assert ui.extension_readiness(two) == {"ready": True, "via": "dialog", "pid": 2}

    def broken():
        raise OSError("no UIA")

    monkeypatch.setattr(ui, "discover_dialogs", broken)
    assert ui.extension_readiness(two)["ready"] is False
