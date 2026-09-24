"""H1 skill registry: discovery, on/off from the settings, refusal of switched-off tools."""
import importlib

import pytest

from willie import skills
from willie.config import Config


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "_config", Config(tmp_path / "willie.yaml"))
    return skills


def test_real_skills_are_found_and_the_template_is_not(registry):
    names = set(registry.available())
    assert {"brandstof", "reminders"} <= names
    assert "_template" not in names
    tools = {d["name"] for d in registry.declarations()}
    assert "gezondheid" in tools and "herinnering_maak" in tools


def test_switching_off_removes_tools_and_refuses_calls(registry):
    from willie.voice import tools
    registry.set_enabled("brandstof", False)
    assert "gezondheid" not in {d["name"] for d in tools.declarations()}
    assert "staat uit" in tools.call("gezondheid", {"onderwerp": "slaap"})["fout"]
    assert registry._config.get("skills.disabled") == "brandstof"
    registry.set_enabled("brandstof", True)
    assert "gezondheid" in {d["name"] for d in tools.declarations()}
    assert {"kijk", "onthoud"} <= {d["name"] for d in tools.declarations()}   # own tools stay


def test_info_lists_cards_and_unknown_skill_is_rejected(registry):
    rows = {r["name"]: r for r in registry.info()}
    assert rows["reminders"]["card"]
    assert rows["brandstof"]["tools"] == ["gezondheid"]
    with pytest.raises(KeyError):
        registry.set_enabled("bestaat_niet", True)


def test_template_is_a_valid_skill():
    template = importlib.import_module("willie.skills._template")
    names = {d["name"] for d in template.DECLARATIONS}
    assert names == set(template.HANDLERS)
    assert template.HANDLERS["voorbeeld"](vraag="x")["antwoord"]
