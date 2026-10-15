from __future__ import annotations

import json
from pathlib import Path

import pytest

from saos.backend.fake import fake_surface


@pytest.fixture
def saos_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SAOS_HOME", str(home))
    monkeypatch.setenv("SAOS_BACKEND", "fake")
    monkeypatch.setenv("SAOS_UI", "0")
    return home


@pytest.fixture
def fake_lens_file(tmp_path: Path) -> Path:
    surfaces = [
        fake_surface(0, thickness=float("inf")),
        fake_surface(
            1,
            radius=50.0,
            thickness=5.0,
            material="N-BK7",
            semi_diameter=10.0,
            is_stop=True,
            solves={"radius": "Variable", "thickness": "Fixed", "material": "Fixed", "semi_diameter": "Automatic", "conic": "Fixed"},
        ),
        fake_surface(
            2,
            radius=-50.0,
            thickness=95.0,
            semi_diameter=10.0,
            solves={"radius": "Variable", "thickness": "MarginalRayHeight", "material": "Fixed", "semi_diameter": "Automatic", "conic": "Fixed"},
        ),
        fake_surface(3, semi_diameter=1.0),
    ]
    path = tmp_path / "source lens.zmx"
    path.write_text(json.dumps({"title": "fake", "surfaces": surfaces}), encoding="utf-8")
    return path
