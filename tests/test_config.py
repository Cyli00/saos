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


def test_the_checkout_finds_its_library_and_skill_and_an_override_wins(monkeypatch, tmp_path):
    monkeypatch.delenv("SAOS_LIBRARY", raising=False)
    assert config.IN_CHECKOUT and config.library_dir() == config.REPO_ROOT / "library"
    assert (config.skill_dir() / "SKILL.md").is_file()
    monkeypatch.setenv("SAOS_LIBRARY", str(tmp_path))
    assert config.library_dir() == tmp_path.resolve()


def test_check_records_keep_their_code_labels_whatever_the_install(monkeypatch):
    # 判定记录里的标签是 src/saos/<文件>：装成包后标签不变，旧任务的判定版本因此不变
    assert config.code_path("src/saos/kit.py") == config.PACKAGE_DIR / "kit.py"
    with pytest.raises(ValueError):
        config.code_path("library/x.py")


def test_the_skill_is_installed_into_another_agents_folder(tmp_path):
    from saos import cli

    assert cli.main(["skill", "--install", str(tmp_path)]) == 0
    assert (tmp_path / "saos" / "SKILL.md").is_file() and (tmp_path / "saos" / "references" / "need.md").is_file()
    assert cli.main(["skill", "--install", str(tmp_path)]) != 0
    assert cli.main(["skill", "--install", str(tmp_path), "--overwrite"]) == 0
