"""D3 test script: the contract every voice adapter must meet.

Runs offline. FakeAdapter is tested directly; GeminiLiveAdapter (D4) is tested against a local
WebSocket server that speaks the Live API wire format and uses a FakeAdapter as its "model", so
the real adapter code (framing, tool calls, interruptions, close) runs without a key. The live
spoken test is `make live-talk` on the Pi.
"""
import asyncio
import base64
import itertools
import json

import pytest
import websockets

from willie.voice.base import Tool, create
from willie.voice.fake import FakeAdapter, tone
from willie.voice.gemini_live import GeminiLiveAdapter

RATE = 16_000
SPEECH = tone(100, RATE, hz=300, level=0.4)        # 100 ms "voice"
SILENCE = bytes(RATE * 100 // 1000 * 2)             # 100 ms room tone


def make_fake(**kw):
    return FakeAdapter(realtime=True, answer_ms=500, chunk_ms=20, **kw)


class MockGeminiServer:
    """Speaks the Gemini Live wire format; a FakeAdapter decides when to answer."""

    def __init__(self, **fake_kw):
        self.fake_kw = fake_kw
        self.setup = None
        self.server = None
        self.url = ""
        self._ids = itertools.count(1)

    async def start(self):
        self.server = await websockets.serve(self._handler, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def _handler(self, ws):
        self.setup = json.loads(await ws.recv())["setup"]
        model = make_fake(**self.fake_kw)
        pending = {}

        async def send(obj):
            try:
                await ws.send(json.dumps(obj))
            except websockets.ConnectionClosed:
                pass

        async def tool_call(name, args):
            call_id = f"call-{next(self._ids)}"
            pending[call_id] = answer = asyncio.get_running_loop().create_future()
            await send({"toolCall": {"functionCalls": [{"id": call_id, "name": name, "args": args}]}})
            return await answer

        def audio(pcm):
            data = base64.b64encode(pcm).decode()
            return send({"serverContent": {"modelTurn": {"parts": [
                {"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": data}}]}}})

        def event(kind, detail):
            if kind == "interrupted":
                return send({"serverContent": {"interrupted": True}})
            if kind == "turn_complete":
                return send({"serverContent": {"turnComplete": True}})

        model.on_audio(audio)
        model.on_event(event)
        model.on_tool_call(tool_call)
        await model.start_session("", "", [])
        await send({"setupComplete": {}})
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if "realtimeInput" in msg:
                    blob = msg["realtimeInput"].get("audio") or msg["realtimeInput"]["mediaChunks"][0]
                    assert blob["mimeType"] == "audio/pcm;rate=16000"
                    await model.send_audio(base64.b64decode(blob["data"]))
                for r in msg.get("toolResponse", {}).get("functionResponses", []):
                    pending.pop(r["id"]).set_result(r["response"])
        except websockets.ConnectionClosed:
            pass
        finally:
            await model.close()


class MockedGemini(GeminiLiveAdapter):
    """The real adapter, pointed at MockGeminiServer."""

    def __init__(self, **fake_kw):
        super().__init__(api_key="test", model="models/gemini-3.8-live", search=True)
        self.mock = MockGeminiServer(**fake_kw)

    async def start_session(self, persona, context, tools):
        await self.mock.start()
        self.url = self.mock.url
        await super().start_session(persona, context, tools)

    async def close(self):
        await super().close()
        if self.mock.server:
            await self.mock.stop()


ADAPTERS = [make_fake, MockedGemini]


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


def test_gemini_setup_message():
    """Persona + context become the system prompt; Tools become functionDeclarations."""
    async def run():
        a = MockedGemini()
        show = Tool("toon", "Show text", {"type": "object", "properties": {"tekst": {"type": "string"}}})
        await a.start_session("PERSONA", "CONTEXT", [show])
        setup = a.mock.setup
        assert setup["model"] == "models/gemini-3.8-live"
        prompt = setup["systemInstruction"]["parts"][0]["text"]
        assert prompt.startswith("PERSONA\n\nSpreek altijd Nederlands") and "CONTEXT" in prompt
        assert prompt.endswith("Je draait op het model gemini-3.8-live.")
        assert {"googleSearch": {}} in setup["tools"]
        assert setup["generationConfig"]["speechConfig"]["languageCode"] == "nl-NL"
        assert setup["tools"][0]["functionDeclarations"][0]["name"] == "toon"
        assert setup["generationConfig"]["responseModalities"] == ["AUDIO"]
        await a.close()
    asyncio.run(run())


def test_gemini_rejected_setup_raises():
    async def run():
        async def refuse(ws):
            await ws.recv()
            await ws.send(json.dumps({"error": {"message": "model not found"}}))
        server = await websockets.serve(refuse, "127.0.0.1", 0)
        a = GeminiLiveAdapter(api_key="x", model="models/nope",
                              url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        with pytest.raises(RuntimeError, match="model not found"):
            await a.start_session("p", "c", [])
        assert not a.is_open
        server.close()
        await server.wait_closed()
    asyncio.run(run())


def test_create_from_config_name():
    assert create("fake").name == "fake"
    assert create("gemini_live", api_key="x").name == "gemini_live"
    with pytest.raises(ValueError):
        create("nope")
