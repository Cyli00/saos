"""显微物镜处方中按注释标记定位物镜段和盖玻片间隔。"""

from __future__ import annotations

import pytest

from saos.protocol import SaosError
from saos.recipes.microscope import layout


def rows(*comments):
    return [{"index": i, "comment": c} for i, c in enumerate(comments)]


def test_objective_block_without_and_with_cover():
    plain = rows("saos:object", "r1", "r2", "saos:stop", "saos:tube", "")
    assert layout.objective_block(plain) == (1, 3)
    assert layout.gap_surface(plain) == 0
    covered = rows("saos:object", "saos:cover_top", "r1", "r2", "saos:tube", "")
    assert layout.objective_block(covered) == (2, 3)
    assert layout.gap_surface(covered) == 1


def test_objective_block_requires_layout():
    with pytest.raises(SaosError):
        layout.objective_block(rows("obj", "r1", "saos:tube", ""))
    with pytest.raises(SaosError):
        layout.objective_block(rows("saos:object", "r1", "r2", ""))
