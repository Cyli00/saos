from __future__ import annotations

import pytest

from saos import protocol


def test_a_failed_write_leaves_the_old_file_and_no_temporary(tmp_path, monkeypatch):
    target = tmp_path / "report" / "index.html"
    protocol.write_text(target, "old")
    assert target.read_text(encoding="utf-8") == "old"

    def refuse(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(protocol.os, "replace", refuse)
    with pytest.raises(OSError):
        protocol.write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "old"
    assert [p.name for p in target.parent.iterdir()] == ["index.html"]
