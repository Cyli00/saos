from __future__ import annotations

import pytest

from saos.recipes.microscope import calls


def test_calls_only_expose_known_ops():
    with pytest.raises(ValueError):
        calls.call("anything", "job")


