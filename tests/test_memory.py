"""Long-term memory (willie/brain/memory.py) and the transcription events it feeds on."""
import asyncio
from datetime import datetime

import pytest

from willie.brain import memory
from willie.voice.gemini_live import GeminiLiveAdapter


@pytest.fixture
def mem(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DIR", tmp_path)
    monkeypatch.setattr(memory, "FACTS", tmp_path / "facts.md")
    monkeypatch.setattr(memory, "SUMMARIES", tmp_path / "summaries.md")
    monkeypatch.setattr(memory, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(memory, "LEGACY", tmp_path / "old_memory.md")
    monkeypatch.setattr(memory, "PENDING", tmp_path / "pending.md")
    return memory


TURNS = [("user", "De Tomos carburateur is een Bing 12 mm."), ("willie", "Genoteerd.")]


def test_note_and_context(mem):
    assert mem.note("Wouter drinkt koffie zwart")["ok"]
    assert mem.note("wouter drinkt koffie zwart")["opmerking"] == "wist ik al"
    assert "koffie zwart" in mem.context()


def test_legacy_file_is_migrated(mem):
    mem.LEGACY.write_text("# Wat WILL-E onthoudt\n\n- 2026-09-21: PETG op 240\n", encoding="utf-8")
    assert "PETG op 240" in mem.context()


def test_digest_saves_words_and_merges_facts(mem, monkeypatch):
    mem.note("Oud feit")
    monkeypatch.setattr(mem, "_merge", lambda text, key: {
        "samenvatting": "Over de Tomos carburateur.",
        "feiten": "# Wat WILL-E weet\n\n## Projecten\n- Oud feit (2026-09-23)\n- Tomos: Bing 12 mm carburateur (2026-09-23)\n"})
    result = mem.digest(TURNS, datetime(2026, 9, 23, 14, 5), api_key="x")
    assert result["samenvatting"] == "Over de Tomos carburateur."
    day = (mem.SESSIONS / "2026-09-23.md").read_text(encoding="utf-8")
    assert "## 14:05" in day and "Wouter: De Tomos carburateur" in day
    assert "Bing 12 mm" in mem.FACTS.read_text(encoding="utf-8")
    ctx = mem.context()
    assert "Bing 12 mm" in ctx and "2026-09-23 14:05: Over de Tomos" in ctx


def test_digest_keeps_facts_when_merge_shrinks_them(mem, monkeypatch):
    for i in range(20):
        mem.note(f"Belangrijk feit nummer {i} dat niet weg mag")
    monkeypatch.setattr(mem, "_merge", lambda text, key: {"samenvatting": "x", "feiten": "- alleen dit"})
    mem.digest(TURNS, api_key="x")
    assert "nummer 19" in mem.FACTS.read_text(encoding="utf-8")


def test_digest_without_key_still_saves_transcript(mem):
    mem.digest(TURNS, datetime(2026, 9, 23, 9, 0), api_key="")
    assert (mem.SESSIONS / "2026-09-23.md").exists()


def test_failed_merge_is_retried_with_the_next_conversation(mem, monkeypatch):
    def busy(text, key):
        raise RuntimeError("HTTP 503")
    monkeypatch.setattr(mem, "_merge", busy)
    mem.digest(TURNS, datetime(2026, 9, 23, 9, 0), api_key="x")
    seen = []
    monkeypatch.setattr(mem, "_merge", lambda text, key: seen.append(text) or {"samenvatting": "s", "feiten": ""})
    mem.digest([("user", "Tweede gesprek")], api_key="x")
    assert "Bing 12 mm" in seen[0] and "Tweede gesprek" in seen[0]
    assert not mem.PENDING.exists()


def test_search_finds_old_conversations(mem):
    mem.save_session(TURNS, datetime(2026, 9, 1, 10, 30))
    found = mem.search("tomos carburateur")["gevonden"]
    assert found and found[0].startswith("[2026-09-01 10:30]")
    assert mem.search("wolfraam")["gevonden"] == []


def test_context_respects_budget(mem):
    for i in range(200):
        with mem.SUMMARIES.open("a", encoding="utf-8") as out:
            out.write(f"- 2026-09-{i % 28 + 1:02d} 10:00: gesprek {i} over van alles en nog wat\n")
    ctx = mem.context(budget=2000)
    assert len(ctx) < 2600 and "gesprek 199" in ctx and "gesprek 0 " not in ctx


def test_adapter_emits_transcriptions_and_asks_for_them():
    adapter = GeminiLiveAdapter(api_key="x")
    events = []

    async def run():
        adapter.on_event(lambda kind, detail: events.append((kind, detail)))
        await adapter._handle({"serverContent": {"inputTranscription": {"text": "hoe laat"}}})
        await adapter._handle({"serverContent": {"outputTranscription": {"text": "Tien uur."}}})
    asyncio.run(run())
    assert ("heard", "hoe laat") in events and ("said", "Tien uur.") in events
    setup = adapter._setup_message("models/gemini-3.8-live", "p")["setup"]
    assert setup["inputAudioTranscription"] == {} and setup["outputAudioTranscription"] == {}
    assert setup["realtimeInputConfig"]["automaticActivityDetection"]["silenceDurationMs"] == 600


def test_standby_sends_nothing_until_hey_willie():
    import io
    from willie.audio import mic
    from willie.voice import gemini_live

    chunk = bytes(1600 * mic.FRAME)                      # 100 ms of stereo silence
    recorder = type("R", (), {"stdout": io.BytesIO(chunk * 8), "terminate": lambda self: None,
                              "wait": lambda self, timeout=None: 0})()
    sent, events = [], []

    class Adapter:
        async def send_audio(self, pcm):
            sent.append(pcm)

    class Spotter:
        calls = 0

        def feed(self, pcm):
            Spotter.calls += 1
            return Spotter.calls == 3                   # "Hey Willie" in the third chunk

    standby = gemini_live.Standby()
    standby.on, standby.spotter = True, Spotter()
    speaker = gemini_live.Speaker()
    asyncio.run(gemini_live._microphone(Adapter(), speaker, asyncio.Event(), [0.0],
                                        lambda k, d: events.append(k), None, recorder, standby))
    assert "wake_again" in events and not standby.on
    # the 3 held chunks (incl. the wake word) go up at once, then the 5 that follow
    assert len(sent) == 3 + 5
