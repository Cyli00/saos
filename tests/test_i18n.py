import pytest

from saos import demand, i18n
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


def test_demand_lang():
    base = {"schema": 2, "objectives": [{"id": "a", "description": "track", "check": {"operand": {"type": "TOTR"}}, "origin": "user"}],
            "summary": "x. " + demand.QUESTION_LINE["en"]}
    assert demand.validate({**base, "lang": "zh"})["lang"] == "zh"
    with pytest.raises(SaosError):
        demand.validate({**base, "lang": "fr"})
    assert demand.digest({**base, "lang": "zh"}) == demand.digest(base)
