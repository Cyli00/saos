from __future__ import annotations

import pytest

from zea.native_ui import choose_window, require_ready
from zea.protocol import ZeaError


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
    with pytest.raises(ZeaError):
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
    with pytest.raises(ZeaError):
        choose_window(windows)
    with pytest.raises(ZeaError):
        choose_window(windows, 3)
    assert choose_window(windows, 2) == windows[1]
    assert choose_window(windows[:1]) == windows[0]
