from __future__ import annotations

import pytest

from zea import config
from zea.protocol import ErrorCode, ZeaError


def test_default_home_is_work_main(monkeypatch):
    monkeypatch.delenv("ZEA_HOME", raising=False)
    monkeypatch.delenv("ZEA_INSTANCE", raising=False)
    assert config.zea_home() == (config.REPO_ROOT / "work" / "main").resolve()


def test_instance_lands_under_work(monkeypatch):
    monkeypatch.delenv("ZEA_HOME", raising=False)
    monkeypatch.setenv("ZEA_INSTANCE", "f1")
    assert config.zea_home() == (config.REPO_ROOT / "work" / "f1").resolve()


def test_explicit_home_wins_over_instance(monkeypatch, tmp_path):
    monkeypatch.setenv("ZEA_HOME", str(tmp_path))
    monkeypatch.setenv("ZEA_INSTANCE", "f1")
    assert config.zea_home() == tmp_path.resolve()


@pytest.mark.parametrize("name", ["../x", "a/b", "-x", "a b", "x" * 49])
def test_bad_instance_name_rejected(monkeypatch, name):
    monkeypatch.delenv("ZEA_HOME", raising=False)
    monkeypatch.setenv("ZEA_INSTANCE", name)
    with pytest.raises(ZeaError) as exc:
        config.zea_home()
    assert exc.value.code == ErrorCode.BAD_ARGS
