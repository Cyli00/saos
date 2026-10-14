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


def test_tile_layout_keeps_windows_apart():
    from saos.native_ui import tile_layout

    area = (0, 200, 3840, 1800)
    out = tile_layout(area, ["Lens Data", "1: Spot Diagram", "2: FFT MTF"])
    assert out["Lens Data"] == (0, 200, 3840, 720)
    assert out["1: Spot Diagram"] == (0, 920, 1920, 1080) and out["2: FFT MTF"] == (1920, 920, 1920, 1080)
    assert tile_layout(area, ["Lens Data"]) == {"Lens Data": area}
    many = tile_layout(area, [f"{k}: A" for k in range(5)])
    rects = list(many.values())
    for i, (x1, y1, w1, h1) in enumerate(rects):
        assert x1 >= 0 and y1 >= 200 and x1 + w1 <= 3840 and y1 + h1 <= 2000
        for x2, y2, w2, h2 in rects[i + 1:]:
            assert x1 + w1 <= x2 or x2 + w2 <= x1 or y1 + h1 <= y2 or y2 + h2 <= y1


def test_pack_places_fixed_size_windows_without_overlap():
    from saos.native_ui import pack

    area = (452, 211, 3388, 1855)
    sizes = [(3388, 742), (900, 819), (2000, 819), (900, 600)]
    spots = pack(area, sizes)
    assert spots[0] == (452, 211) and spots[1] == (452, 953) and spots[2] == (1352, 953) and spots[3] == (452, 1772)
    rects = [(x, y, w, h) for (x, y), (w, h) in zip(spots, sizes)]
    for i, (x1, y1, w1, h1) in enumerate(rects):
        for x2, y2, w2, h2 in rects[i + 1:]:
            assert x1 + w1 <= x2 or x2 + w2 <= x1 or y1 + h1 <= y2 or y2 + h2 <= y1
