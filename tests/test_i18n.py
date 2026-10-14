import pytest

from saos import harness, i18n
from saos.protocol import SaosError


@pytest.mark.parametrize(
    ("value", "lang"),
    [("zh-CN", "zh"), ("zh_TW.UTF-8", "zh"), ("Chinese (Simplified)_China", "zh"), ("en_US", "en"), ("English_United States", "en"), ("de_DE", None), ("", None), (None, None)],
)
def test_normalize(value, lang):
    assert i18n.normalize(value) == lang


def test_resolve_priority(monkeypatch):
    monkeypatch.setattr(i18n, "system_lang", lambda: "zh")
    monkeypatch.delenv("SAOS_LANG", raising=False)
    assert i18n.resolve(None, None) == "zh"
    monkeypatch.setenv("SAOS_LANG", "en")
    assert i18n.resolve(None, None) == "en"
    assert i18n.resolve(None, "zh") == "zh"
    assert i18n.resolve("en", "zh") == "en"
    monkeypatch.delenv("SAOS_LANG")
    monkeypatch.setattr(i18n, "system_lang", lambda: None)
    assert i18n.resolve() == i18n.DEFAULT


def test_harness_lang():
    base = {"schema": 1, "request": "x", "targets": [{"id": "a", "kind": "preference", "check": {"operand": {"type": "TOTR"}}}]}
    assert harness.validate({**base, "lang": "zh"})["lang"] == "zh"
    with pytest.raises(SaosError):
        harness.validate({**base, "lang": "fr"})
    assert harness.digest({**base, "lang": "zh"}) == harness.digest(base)
