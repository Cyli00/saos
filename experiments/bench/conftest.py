from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def saos_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SAOS_HOME", str(home))
    monkeypatch.setenv("SAOS_BACKEND", "fake")
    return home
