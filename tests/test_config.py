from __future__ import annotations

import pytest

from saos import config
from saos.protocol import ErrorCode, SaosError


def test_default_home_is_work_main(monkeypatch):
    monkeypatch.delenv("SAOS_HOME", raising=False)
    monkeypatch.delenv("SAOS_INSTANCE", raising=False)
    assert config.saos_home() == (config.REPO_ROOT / "work" / "main").resolve()


def test_instance_lands_under_work(monkeypatch):
    monkeypatch.delenv("SAOS_HOME", raising=False)
    monkeypatch.setenv("SAOS_INSTANCE", "f1")
    assert config.saos_home() == (config.REPO_ROOT / "work" / "f1").resolve()


def test_explicit_home_wins_over_instance(monkeypatch, tmp_path):
    monkeypatch.setenv("SAOS_HOME", str(tmp_path))
    monkeypatch.setenv("SAOS_INSTANCE", "f1")
    assert config.saos_home() == tmp_path.resolve()


@pytest.mark.parametrize("name", ["../x", "a/b", "-x", "a b", "x" * 49])
def test_bad_instance_name_rejected(monkeypatch, name):
    monkeypatch.delenv("SAOS_HOME", raising=False)
    monkeypatch.setenv("SAOS_INSTANCE", name)
    with pytest.raises(SaosError) as exc:
        config.saos_home()
    assert exc.value.code == ErrorCode.BAD_ARGS
