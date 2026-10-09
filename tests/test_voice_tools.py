"""Voice-triggered microphone mute (D17): the Live session finds the action through the
tool declaration (there is no separate NLU step - the model picks a tool by its
description, same as every other skill, §6), then the handler itself gates and performs it.
"""
from __future__ import annotations

import pytest

import willie.config as config_module
from willie.voice import tools


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    live = config_module.Config(tmp_path / "willie.yaml")
    monkeypatch.setattr(config_module, "Config", lambda *a, **k: live)
    return live


def test_demp_microfoon_is_declared_with_the_spoken_trigger_phrases():
    decl = next(d for d in tools.DECLARATIONS if d["name"] == "demp_microfoon")
    assert decl["parameters"]["required"] == ["bevestigd"]
    assert "mute microfoon" in decl["description"]
    assert "zet je microfoon uit" in decl["description"]


def test_call_routes_to_the_handler_by_name():
    # The dispatcher the Live session actually calls (tools.call), not the function directly.
    assert tools.HANDLERS["demp_microfoon"] is tools.demp_microfoon
    result = tools.call("demp_microfoon", {"bevestigd": False})
    assert "eerst_vragen" in result


def test_without_confirmation_nothing_is_muted(cfg, monkeypatch):
    silenced = []
    monkeypatch.setattr(tools, "MUTE_HOOK", None)
    monkeypatch.setattr("willie.audio.speech.silence", lambda on: silenced.append(on))

    result = tools.demp_microfoon(bevestigd=False)

    assert "eerst_vragen" in result
    assert cfg.get("privacy.mute") is False
    assert silenced == []


def test_confirmed_mute_sets_privacy_mute_silences_and_calls_the_hook(cfg, monkeypatch):
    silenced = []
    hooked = []
    monkeypatch.setattr("willie.audio.speech.silence", lambda on: silenced.append(on))
    monkeypatch.setattr(tools, "MUTE_HOOK", lambda: hooked.append(True))

    result = tools.demp_microfoon(bevestigd=True)

    assert result["ok"] is True
    assert cfg.get("privacy.mute") is True
    assert silenced == [True]
    assert hooked == [True]


def test_a_failing_hook_does_not_break_the_mute(cfg, monkeypatch):
    monkeypatch.setattr("willie.audio.speech.silence", lambda on: None)

    def boom():
        raise RuntimeError("face gone")

    monkeypatch.setattr(tools, "MUTE_HOOK", boom)

    result = tools.demp_microfoon(bevestigd=True)

    assert result["ok"] is True
    assert cfg.get("privacy.mute") is True
