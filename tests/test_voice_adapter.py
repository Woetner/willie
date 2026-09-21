"""D3 test script: the contract every voice adapter must meet.

Runs offline against FakeAdapter. D4/D5 add Gemini Live / OpenAI Realtime to ADAPTERS (those need
a key and a recorded question, so they will be marked and skipped without one).
"""
import asyncio

import pytest

from willie.voice.base import Tool, create
from willie.voice.fake import FakeAdapter, tone

RATE = 16_000
SPEECH = tone(100, RATE, hz=300, level=0.4)        # 100 ms "voice"
SILENCE = bytes(RATE * 100 // 1000 * 2)             # 100 ms room tone


def make_fake(**kw):
    return FakeAdapter(realtime=True, answer_ms=500, chunk_ms=20, **kw)


ADAPTERS = [make_fake]


class Recorder:
    def __init__(self, adapter):
        self.events, self.audio = [], []
        adapter.on_event(lambda k, d: self.events.append(k))
        adapter.on_audio(self.audio.append)

    def kinds(self):
        return list(self.events)


async def say(adapter, speech_chunks=5, silence_chunks=5):
    for _ in range(speech_chunks):
        await adapter.send_audio(SPEECH)
    for _ in range(silence_chunks):
        await adapter.send_audio(SILENCE)


async def wait_for(pred, timeout=3.0):
    end = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


@pytest.mark.parametrize("factory", ADAPTERS)
def test_conversation_turn(factory):
    async def run():
        a = factory()
        rec = Recorder(a)
        await a.start_session("persona text", "mood: curious; battery 64 %", [])
        assert rec.kinds() == ["ready"] and a.is_open
        await say(a)
        await wait_for(lambda: "turn_complete" in rec.events)
        assert rec.events.index("speaking") < rec.events.index("turn_complete")
        assert sum(len(c) for c in rec.audio) > a.out_rate // 10 * 2   # > 100 ms of answer
        await a.close()
        await a.close()                                              # twice is fine
        assert rec.events[-1] == "closed" and rec.events.count("closed") == 1
        with pytest.raises(ConnectionError):
            await a.send_audio(SPEECH)
    asyncio.run(run())


@pytest.mark.parametrize("factory", ADAPTERS)
def test_tool_call_show(factory):
    """D4's done-when needs one tool call (`show()`): handler path and callback path."""
    async def run():
        shown = []
        show = Tool("show", "Show text on the face", {"type": "object", "properties": {"text": {"type": "string"}}},
                    handler=lambda args: shown.append(args["text"]) or {"ok": True})
        a = factory(script=[{"tool": "show", "args": {"text": "M3 = 0.5 Nm"}}, {"tool": "show", "args": {"text": "x"}}])
        rec = Recorder(a)
        await a.start_session("p", "c", [show])
        await say(a)
        await wait_for(lambda: "turn_complete" in rec.events)
        assert shown == ["M3 = 0.5 Nm"] and "tool" in rec.events

        calls = []
        a.on_tool_call(lambda name, args: calls.append((name, args)) or {"ok": "callback"})
        await say(a)
        await wait_for(lambda: rec.events.count("turn_complete") == 2)
        assert calls == [("show", {"text": "x"})] and shown == ["M3 = 0.5 Nm"]
        await a.close()
    asyncio.run(run())


@pytest.mark.parametrize("factory", ADAPTERS)
def test_broken_tool_does_not_end_session(factory):
    async def run():
        def boom(args):
            raise RuntimeError("camera busy")
        a = factory(script=[{"tool": "kijk"}])
        rec = Recorder(a)
        await a.start_session("p", "c", [Tool("kijk", "look", handler=boom)])
        await say(a)
        await wait_for(lambda: "turn_complete" in rec.events)
        assert "error" in rec.events and a.is_open
        await a.close()
    asyncio.run(run())


@pytest.mark.parametrize("factory", ADAPTERS)
def test_barge_in_and_interrupt(factory):
    async def run():
        a = factory()
        rec = Recorder(a)
        await a.start_session("p", "c", [])
        await say(a)
        await wait_for(lambda: "speaking" in rec.events)
        await a.send_audio(SPEECH)                       # the user talks over him
        await wait_for(lambda: "interrupted" in rec.events)
        n = len(rec.audio)
        await asyncio.sleep(0.1)
        assert len(rec.audio) == n, "audio kept coming after barge-in"
        assert "turn_complete" not in rec.events

        for _ in range(4):                               # finish that user turn -> new answer
            await a.send_audio(SILENCE)
        await wait_for(lambda: rec.events.count("speaking") == 2)
        await a.interrupt()                              # local interrupt (e.g. "stop")
        assert rec.events.count("interrupted") == 2
        await a.close()
    asyncio.run(run())


def test_create_from_config_name():
    assert create("fake").name == "fake"
    with pytest.raises(ValueError):
        create("nope")
