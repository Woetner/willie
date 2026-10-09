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
from willie.voice.openai_realtime import OpenAIRealtimeAdapter, to_meter, upsample

from test_cascade import make_cascade        # adapter C: the same contract, with fake parts

RATE = 16_000
SPEECH = tone(100, RATE, hz=300, level=0.4)        # 100 ms "voice"
SILENCE = bytes(RATE * 100 // 1000 * 2)             # 100 ms room tone


# One real 3.8-live turn (probe, 25 Sep).
USAGE = {"promptTokenCount": 3867, "responseTokenCount": 116, "totalTokenCount": 3983,
         "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 3620}, {"modality": "AUDIO", "tokenCount": 222}],
         "responseTokensDetails": [{"modality": "AUDIO", "tokenCount": 116}], "thoughtsTokenCount": 197}


def make_fake(**kw):
    return FakeAdapter(realtime=True, answer_ms=500, chunk_ms=20, **kw)


class MockGeminiServer:
    """Speaks the Gemini Live wire format; a FakeAdapter decides when to answer."""

    def __init__(self, **fake_kw):
        self.fake_kw = fake_kw
        self.setup = None
        self.texts, self.stream_ends = [], 0
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
                # As measured on 3.8-live (25 Sep): usage rides on the turnComplete message.
                return send({"serverContent": {"turnComplete": True}, "usageMetadata": USAGE})

        model.on_audio(audio)
        model.on_event(event)
        model.on_tool_call(tool_call)
        await model.start_session("", "", [])
        await send({"setupComplete": {}})
        if "sessionResumption" in self.setup:
            await send({"sessionResumptionUpdate": {"newHandle": "handle-2", "resumable": True}})
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if "clientContent" in msg:
                    self.texts.append(msg["clientContent"]["turns"][0]["parts"][0]["text"])
                if "realtimeInput" in msg and msg["realtimeInput"].get("audioStreamEnd"):
                    self.stream_ends += 1
                elif "realtimeInput" in msg:
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


# One gpt-realtime turn in the shape of the API reference (9 Oct); not measured yet.
OPENAI_USAGE = {"input_tokens": 4000, "output_tokens": 130,
                "input_token_details": {"text_tokens": 3700, "audio_tokens": 300, "cached_tokens": 3000,
                                        "cached_tokens_details": {"text_tokens": 3000, "audio_tokens": 0}},
                "output_token_details": {"text_tokens": 30, "audio_tokens": 100}}


class MockOpenAIServer:
    """Speaks the OpenAI Realtime wire format; a FakeAdapter decides when to answer."""

    def __init__(self, **fake_kw):
        self.fake_kw = fake_kw
        self.session, self.auth = None, ""
        self.texts, self.creates = [], 0
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
        self.auth = ws.request.headers.get("Authorization", "")
        model = make_fake(**self.fake_kw)
        model.in_rate = 24_000
        pending = {}

        async def send(obj):
            try:
                await ws.send(json.dumps(obj))
            except websockets.ConnectionClosed:
                pass

        def done(status="completed", output=()):
            return send({"type": "response.done", "response": {"status": status, "output": list(output),
                                                               "usage": OPENAI_USAGE}})

        async def tool_call(name, args):
            call_id = f"call-{next(self._ids)}"
            pending[call_id] = answer = asyncio.get_running_loop().create_future()
            await done(output=[{"type": "function_call", "name": name, "call_id": call_id,
                                "arguments": json.dumps(args)}])
            return json.loads(await answer)

        def audio(pcm):
            return send({"type": "response.output_audio.delta", "delta": base64.b64encode(pcm).decode()})

        async def event(kind, detail):
            if kind == "interrupted":
                await send({"type": "input_audio_buffer.speech_started"})
                await done("cancelled")
            if kind == "turn_complete":
                await done()

        model.on_audio(audio)
        model.on_event(event)
        model.on_tool_call(tool_call)
        await model.start_session("", "", [])
        await send({"type": "session.created", "session": {}})
        try:
            async for raw in ws:
                msg = json.loads(raw)
                kind = msg["type"]
                if kind == "session.update":
                    self.session = msg["session"]
                    await send({"type": "session.updated", "session": self.session})
                elif kind == "input_audio_buffer.append":
                    await model.send_audio(base64.b64decode(msg["audio"]))
                elif kind == "response.cancel":
                    await model.interrupt()
                elif kind == "response.create":
                    self.creates += 1
                elif kind == "conversation.item.create" and msg["item"]["type"] == "function_call_output":
                    pending.pop(msg["item"]["call_id"]).set_result(msg["item"]["output"])
                elif kind == "conversation.item.create":
                    self.texts.append(msg["item"]["content"][0]["text"])
        except websockets.ConnectionClosed:
            pass
        finally:
            await model.close()


class MockedOpenAI(OpenAIRealtimeAdapter):
    """The real adapter, pointed at MockOpenAIServer."""

    def __init__(self, **fake_kw):
        super().__init__(api_key="test", model="gpt-realtime-2.1")
        self.mock = MockOpenAIServer(**fake_kw)

    async def start_session(self, persona, context, tools):
        await self.mock.start()
        self.url = self.mock.url
        await super().start_session(persona, context, tools)

    async def close(self):
        await super().close()
        if self.mock.server:
            await self.mock.stop()


ADAPTERS = [make_fake, MockedGemini, MockedOpenAI, make_cascade]


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


def test_openai_setup_message():
    """Persona + context become the instructions; Tools become function tools; 24 kHz both ways."""
    async def run():
        a = MockedOpenAI(script=[{"tool": "toon", "args": {"tekst": "hoi"}}])
        rec = Recorder(a)
        show = Tool("toon", "Show text", {"type": "object", "properties": {"tekst": {"type": "string"}}},
                    handler=lambda args: {"ok": True})
        await a.start_session("PERSONA", "CONTEXT", [show])
        session = a.mock.session
        assert a.mock.auth == "Bearer test" and session["type"] == "realtime"
        assert session["instructions"].startswith("PERSONA\n\nTool rules.")
        assert "CONTEXT\n\nSpreek altijd Nederlands" in session["instructions"]
        assert session["instructions"].endswith("Je draait op het model gpt-realtime-2.1.")
        assert session["tools"] == [{"type": "function", **show.declaration()}]
        audio = session["audio"]
        assert audio["input"]["format"] == audio["output"]["format"] == {"type": "audio/pcm", "rate": 24000}
        assert audio["input"]["transcription"]["language"] == "nl" and audio["output"]["voice"] == "cedar"
        assert not a.search                              # no built-in search: zoek_op stays offered
        await say(a)
        await wait_for(lambda: "turn_complete" in rec.events)
        assert a.mock.creates == 1                       # tool results in -> "go on"
        await a.send_text("[SCHERM] MEER")
        await wait_for(lambda: a.mock.creates == 2)
        assert a.mock.texts == ["[SCHERM] MEER"]
        await a.close()
    asyncio.run(run())


def test_openai_dutch_in_english_out():
    """voice.language nl_en: his words are transcribed as Dutch, the prompt orders English answers."""
    async def run():
        a = MockedOpenAI()
        a.language = "nl_en"
        await a.start_session("PERSONA", "CONTEXT", [])
        session = a.mock.session
        assert session["audio"]["input"]["transcription"]["language"] == "nl"
        assert "ALWAYS answer in English" in session["instructions"]
        await a.close()
    asyncio.run(run())
    assert GeminiLiveAdapter(api_key="x", language="nl_en").language == "nl"


def test_openai_rate_limit_asks_again(monkeypatch):
    """A response refused for the rate limit is asked again instead of leaving him silent."""
    from willie.voice import openai_realtime
    monkeypatch.setattr(openai_realtime, "LIMIT_WAIT_S", 0.01)

    async def run():
        a = OpenAIRealtimeAdapter(api_key="x")
        rec, sent = Recorder(a), []

        async def send(message):
            sent.append(message["type"])
        a._send = send
        failed = {"type": "response.done", "response": {"status": "failed", "status_details": {"error": {
            "message": "Rate limit reached for gpt-realtime-2.1-mini on tokens per min (TPM)"}}}}
        for _ in range(openai_realtime.LIMIT_RETRIES):
            await a._handle(failed)
            await asyncio.sleep(0.05)
        assert sent == ["response.create"] * openai_realtime.LIMIT_RETRIES and "error" not in rec.events
        await a._handle(failed)                          # still refused: now it is an error
        assert rec.events == ["error"]
    asyncio.run(run())


def test_openai_resamples_the_mic_to_24k():
    first, last = upsample(SPEECH)
    out = first + upsample(SPEECH, last)[0]
    assert len(out) == len(SPEECH) * 2 * 3 // 2
    assert 0.35 < max(abs(v) for v in memoryview(out).cast("h")) / 32768 <= 0.4


def test_openai_usage_reaches_the_meter(monkeypatch):
    from willie import usage
    counts = usage.add({}, to_meter(OPENAI_USAGE))
    # 700 fresh text + 3000 cached at a tenth of the text price.
    assert counts["prompt_text"] == 1000 and counts["prompt_audio"] == 300
    assert counts["out_text"] == 30 and counts["out_audio"] == 100
    assert usage.family("gpt-realtime-2.1") == "realtime"
    assert usage.usd("gpt-realtime-2.1", counts) == pytest.approx((1000 * 4 + 300 * 32 + 30 * 24 + 100 * 64) / 1e6)


def test_openai_rejected_setup_raises():
    async def run():
        async def refuse(ws):
            await ws.recv()
            await ws.send(json.dumps({"type": "error", "error": {"message": "model not found"}}))
        server = await websockets.serve(refuse, "127.0.0.1", 0)
        a = OpenAIRealtimeAdapter(api_key="x", model="gpt-nope",
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
    assert create("openai_realtime", api_key="x").name == "openai_realtime"
    with pytest.raises(ValueError):
        create("nope")


def test_web_search_tool_uses_a_second_live_session():
    """zoek_op: a 2.5 Live session with Google Search; only its transcript comes back."""
    from willie.voice import search

    async def run():
        seen = {}

        async def fake_25(ws):
            seen["setup"] = json.loads(await ws.recv())["setup"]
            await ws.send(json.dumps({"setupComplete": {}}))
            seen["question"] = json.loads(await ws.recv())["clientContent"]["turns"][0]["parts"][0]["text"]
            await ws.send(json.dumps({"serverContent": {"groundingMetadata": {"webSearchQueries": ["x"]}}}))
            for piece in ("De zon gaat ", "om 19:44 onder."):
                await ws.send(json.dumps({"serverContent": {"outputTranscription": {"text": piece}}}))
            await ws.send(json.dumps({"serverContent": {"turnComplete": True}}))
            await ws.wait_closed()

        server = await websockets.serve(fake_25, "127.0.0.1", 0)
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        result = await search.search("Zonsondergang Haarlem vandaag?", url=url)
        assert result == {"antwoord": "De zon gaat om 19:44 onder.", "gezocht": True}
        assert seen["question"] == "Zonsondergang Haarlem vandaag?"
        assert {"googleSearch": {}} in seen["setup"]["tools"] and "outputAudioTranscription" in seen["setup"]
        assert (await search.search("  ")) == {"fout": "geen vraag"}
        server.close()
        await server.wait_closed()
    asyncio.run(run())


def test_web_search_tool_only_without_native_search():
    from willie.voice import gemini_live
    names = lambda **kw: [t.name for t in gemini_live.willie_tool_list(**kw)]
    assert "zoek_op" in names() and "zoek_op" not in names(web_search=False)


def test_gemini_resumption_text_and_stream_end():
    """Garage mode (K1): the setup asks for resumption, the newest handle is kept, and the
    robot's own messages and audioStreamEnd reach the server."""
    async def main():
        adapter = MockedGemini()
        adapter.resume, adapter.resume_handle = True, "handle-1"
        await adapter.start_session("persona", "", [])
        setup = adapter.mock.setup
        assert setup["sessionResumption"] == {"handle": "handle-1"}
        assert "slidingWindow" in setup["contextWindowCompression"]
        await wait_for(lambda: adapter.resume_handle == "handle-2")
        await adapter.send_text("[timer] de lijm is droog")
        await adapter.end_audio()
        await wait_for(lambda: adapter.mock.texts and adapter.mock.stream_ends == 1)
        assert adapter.mock.texts == ["[timer] de lijm is droog"]
        await adapter.close()
    asyncio.run(main())


def test_session_usage_reaches_the_meter(monkeypatch):
    """J3: one record per session, summed over its turns, sent when it closes."""
    from willie import usage
    records = []
    monkeypatch.setattr(usage, "HOOK", records.append)

    async def run():
        a = MockedGemini()
        rec = Recorder(a)
        await a.start_session("persona", "", [])
        await say(a)
        await wait_for(lambda: "turn_complete" in rec.events)
        assert records == []                        # nothing before the session ends
        await a.close()
        await a.close()
    asyncio.run(run())
    assert len(records) == 1
    r = records[0]
    assert r["source"] == "robot" and r["model"] == "gemini-3.8-live" and r["turns"] == 1
    assert (r["prompt_text"], r["prompt_audio"], r["out_audio"], r["thoughts"]) == (3645, 222, 116, 197)


def test_followup_window_follows_his_last_answer():
    """T3: longer after his own question, shorter after a confirmed command."""
    from willie.voice.gemini_live import followup_window
    windows = (8.0, 30.0)
    assert followup_window(20, "Is dat helder, of zal ik een stuk uitdiepen?", False, windows) == 30
    assert followup_window(20, "Gedaan.", True, windows) == 8
    assert followup_window(20, "Nozzle 240, bed 80.", False, windows) == 20
    assert followup_window(20, "Ik zie een pet op tafel liggen, vlak bij wat schillen en een appel.", True, windows) == 20
    assert followup_window(None, "Gedaan.", True, windows) is None


def test_context_cap_leaves_room_for_the_conversation():
    """9 Oct: the setup grew past the cap, the server dropped every turn and he forgot the question."""
    from willie.voice.gemini_live import CHARS_PER_TOKEN, TALK_KEEP_TOKENS, TALK_ROOM_TOKENS, fit_window
    small = {"systemInstruction": "x" * 2800}                         # ~1k tokens: the settings hold
    assert fit_window((12000, 8000), small) == (12000, 8000)
    big = {"systemInstruction": "x" * int(12000 * CHARS_PER_TOKEN)}   # ~12k tokens: over the cap by itself
    trigger, target = fit_window((12000, 8000), big)
    assert target >= 12000 + TALK_KEEP_TOKENS and trigger == target + TALK_ROOM_TOKENS
