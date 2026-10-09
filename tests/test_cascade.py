"""Adapter C (willie/voice/cascade.py): speech-to-text -> text model -> text-to-speech.

Offline. The adapter's own logic runs against three fake parts (they also carry it through
the D3 contract in test_voice_adapter.py); the real Ears, Brain and Mouth run against local
servers that speak the wire formats, so the framing, the key fallback and the streaming are
tested without a key.
"""
import asyncio
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import websockets

from willie import usage
from willie.voice import cascade
from willie.voice.base import Tool
from willie.voice.cascade import Brain, CascadeAdapter, Ears, Mouth, Sentences, StageError
from willie.voice.fake import peak, tone

SPEECH = tone(100, 16_000, level=0.4)
SILENCE = bytes(3200)


# ---------------------------------------------------------------- fake parts
class FakeEars:
    """A server-side voice detector: loud = speech, 300 ms of quiet = the sentence is over."""
    model = "fake-stt"

    def __init__(self, texts=None, guesses=None, text_after=0.05):
        self.texts, self.guesses, self.text_after = list(texts or []), list(guesses or []), text_after
        self.talking, self.quiet_ms, self.event, self.usage = False, 0.0, None, {}

    async def open(self, on_event):
        self.event = on_event

    async def send(self, pcm):
        if peak(pcm) >= 0.05:
            self.quiet_ms = 0
            if not self.talking:
                self.talking = True
                await self.event("speech_started", "")
        elif self.talking:
            self.quiet_ms += len(pcm) / 32
            if self.quiet_ms >= 300:
                self.talking = False
                await self.event("speech_stopped", "")
                text = self.texts.pop(0) if self.texts else "Hoe laat is het?"
                if self.guesses:                     # the quick start: a guess now, the text a moment later
                    await self.event("guess", self.guesses.pop(0))
                    self.pending = asyncio.get_running_loop().call_later(
                        self.text_after, lambda: asyncio.ensure_future(self.event("text", text)))
                else:
                    await self.event("text", text)

    async def close(self):
        pass


class FakeBrain:
    """`script`: one dict per question, {"tool": name, "args": {...}} or {"text": "..."}."""
    key_kind = "betaald"

    def __init__(self, script=None, model="fake-llm", fail=False, delay=0.0):
        self.script, self.model, self.fail, self.delay = list(script or []), model, fail, delay
        self.seen: list[list[dict]] = []

    async def stream(self, messages, tools):
        self.seen.append([dict(m) for m in messages])
        if self.fail:
            raise StageError("brain down", 503)
        await asyncio.sleep(self.delay)
        if messages[-1]["role"] == "tool":
            yield "text", "Dat is gedaan. "
        else:
            turn = self.script.pop(0) if self.script else {}
            if turn.get("tool"):
                yield "calls", [{"id": "call_1", "type": "function",
                                 "function": {"name": turn["tool"], "arguments": json.dumps(turn.get("args", {}))}}]
            else:
                for word in turn.get("text", "Het is kwart voor twee. Nog iets anders dan dit?").split(" "):
                    yield "text", word + " "
        yield "usage", {"promptTokensDetails": [{"modality": "TEXT", "tokenCount": 100}],
                        "responseTokensDetails": [{"modality": "TEXT", "tokenCount": 10}]}


class FakeMouth:
    model, provider, rate = "fake-tts", "fake", 24_000

    def __init__(self, fail=False, ms=500):
        self.fail, self.ms, self.said = fail, ms, []

    async def stream(self, text):
        if self.fail:
            raise StageError("mouth down", 503)
        self.said.append(text)
        pcm = tone(self.ms, self.rate)
        for at in range(0, len(pcm), 960):
            yield pcm[at:at + 960]
            await asyncio.sleep(0.02)


def make_cascade(script=None, **kw):
    """The D3 contract's factory (test_voice_adapter.ADAPTERS)."""
    return CascadeAdapter(ears=FakeEars(kw.pop("texts", None)), brains=[FakeBrain(script)],
                          mouths=[FakeMouth()], noise_filter=False, **kw)


class Recorder:
    def __init__(self, adapter):
        self.events, self.audio = [], []
        adapter.on_event(lambda kind, detail: self.events.append((kind, detail)))
        adapter.on_audio(self.audio.append)

    def kinds(self):
        return [kind for kind, _ in self.events]

    def details(self, kind):
        return [detail for k, detail in self.events if k == kind]


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


# ---------------------------------------------------------------- the adapter
def test_a_turn_is_heard_answered_per_sentence_and_remembered():
    async def run():
        a = make_cascade(texts=["Hoe laat is het?"])
        rec = Recorder(a)
        await a.start_session("PERSONA", "CONTEXT", [])
        await say(a)
        await wait_for(lambda: "turn_complete" in rec.kinds())
        assert rec.details("heard") == ["Hoe laat is het?"]
        # The first sentence alone (heard soonest), the rest in one request.
        assert a.mouths[0].said == ["Het is kwart voor twee.", "Nog iets anders dan dit?"]
        assert "".join(rec.details("said")) == "Het is kwart voor twee. Nog iets anders dan dit? "
        assert [m["role"] for m in a.messages] == ["system", "user", "assistant"]
        system = a.messages[0]["content"]
        assert system.startswith("PERSONA") and "CONTEXT" in system and "read aloud" in system
        assert "Spreek altijd Nederlands" in system and "fake-llm" in system
        await a.close()
    asyncio.run(run())


def test_english_out_is_said_first_and_last():
    async def run():
        a = make_cascade(language="nl_en")
        await a.start_session("PERSONA", "", [])
        system = a.messages[0]["content"]
        assert system.startswith("OUTPUT LANGUAGE: English only") and "ALWAYS answer in English" in system
        tool = Tool("huis_nu", "Home", handler=lambda args: {"nu": "Alles rustig."})
        a._register_tools([tool])
        answer = await a._tool({"id": "c", "function": {"name": "huis_nu", "arguments": "{}"}})
        assert "Speak English only" in answer["content"] and "Alles rustig." in answer["content"]
        await a.close()
    asyncio.run(run())


def test_a_tool_result_goes_back_to_the_model():
    async def run():
        shown = []
        show = Tool("toon", "Show text", {"type": "object", "properties": {"tekst": {"type": "string"}}},
                    handler=lambda args: shown.append(args["tekst"]) or {"ok": True})
        a = make_cascade(script=[{"tool": "toon", "args": {"tekst": "42"}}])
        rec = Recorder(a)
        await a.start_session("p", "c", [show])
        await say(a)
        await wait_for(lambda: "turn_complete" in rec.kinds())
        assert shown == ["42"]
        assert [m["role"] for m in a.messages] == ["system", "user", "assistant", "tool", "assistant"]
        assert a.messages[2]["tool_calls"][0]["function"]["name"] == "toon"
        assert a.messages[3] == {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'}
        assert a.brains[0].seen[0][0]["role"] == "system" and len(a.brains[0].seen) == 2
        await a.close()
    asyncio.run(run())


def test_going_on_after_a_pause_is_one_question():
    """The turn detection cut in at a pause; he was still thinking, so he starts over with all of it."""
    async def run():
        a = CascadeAdapter(ears=FakeEars(["Zet de radio", "op Radio 10."]), brains=[FakeBrain(delay=0.3)],
                           mouths=[FakeMouth()], noise_filter=False)
        rec = Recorder(a)
        await a.start_session("p", "c", [])
        await say(a)
        await say(a)                                   # before the first answer made a sound
        await wait_for(lambda: "turn_complete" in rec.kinds())
        assert [m["content"] for m in a.messages if m["role"] == "user"] == ["Zet de radio op Radio 10."]
        assert rec.kinds().count("turn_complete") == 1 and "interrupted" not in rec.kinds()
        await a.close()
    asyncio.run(run())


def test_a_right_guess_starts_the_answer_early_and_a_wrong_one_is_never_heard():
    async def run():
        done = []
        tool = Tool("toon", "Show", handler=lambda args: done.append(args) or {"ok": True})
        # 1: guessed right (only the writing differs). 2: guessed wrong - that answer would have
        # called a tool. 3: a guess that is never confirmed.
        ears = FakeEars(["Zet Radio-10 aan.", "Hoe laat is het?", "x"], ["zet radio 10 aan", "Hoe laat", "Koffie"])
        brain = FakeBrain([{"text": "Radio 10 staat aan."}, {"tool": "toon", "args": {"tekst": "fout"}}, {"text": "Kwart voor twee."}])
        a = CascadeAdapter(ears=ears, brains=[brain], mouths=[FakeMouth(ms=100)], noise_filter=False)
        rec = Recorder(a)
        await a.start_session("p", "c", [tool])
        await say(a)
        await wait_for(lambda: rec.kinds().count("turn_complete") == 1)
        assert brain.seen[0][-1]["content"] == "zet radio 10 aan"       # the brain was already at work on the guess
        assert rec.details("heard") == ["Zet Radio-10 aan."] and len(brain.seen) == 1

        ears.text_after = 0.3                         # the wrong answer is long written before the text arrives
        await say(a)
        await wait_for(lambda: rec.kinds().count("turn_complete") == 2)
        assert done == [] and "tool" not in rec.kinds()                # the tool of the wrong guess never ran
        assert [m["content"] for m in a.messages if m["role"] == "user"] == ["zet radio 10 aan", "Hoe laat is het?"]
        assert "".join(rec.details("said")) == "Radio 10 staat aan. Kwart voor twee. "

        monkey_confirm, cascade.CONFIRM_S = cascade.CONFIRM_S, 0.2
        try:
            ears.text_after = 5
            await say(a)
            await asyncio.sleep(0.5)
            assert rec.kinds().count("turn_complete") == 2 and "error" not in rec.kinds()
            assert len([m for m in a.messages if m["role"] == "user"]) == 2
        finally:
            cascade.CONFIRM_S = monkey_confirm
            ears.pending.cancel()
        await a.close()
    asyncio.run(run())


def test_he_waits_until_wouter_stops_talking():
    async def run():
        a = make_cascade()
        rec = Recorder(a)
        await a.start_session("p", "c", [])
        await say(a)
        await a.send_audio(SPEECH)                    # Wouter goes on while the answer is being made
        await asyncio.sleep(0.3)
        assert "speaking" not in rec.kinds()
        for _ in range(4):
            await a.send_audio(SILENCE)
        await wait_for(lambda: "turn_complete" in rec.kinds())
        assert len([m for m in a.messages if m["role"] == "user"]) == 1 and rec.kinds().count("turn_complete") == 1
        await a.close()
    asyncio.run(run())


def test_a_queued_message_is_not_lost_when_wouter_talks():
    async def run():
        a = CascadeAdapter(ears=FakeEars(["Eerste vraag.", "Tweede vraag."]), brains=[FakeBrain(delay=0.2)],
                           mouths=[FakeMouth(ms=100)], noise_filter=False)
        rec = Recorder(a)
        await a.start_session("p", "c", [])
        await say(a)
        await wait_for(lambda: "speaking" in rec.kinds())
        await a.send_text("[timer] klaar")           # waits behind the answer he is giving
        await say(a)                                 # and Wouter talks over that answer
        await wait_for(lambda: "turn_complete" in rec.kinds())
        assert a.messages[0]["role"] == "system"
        assert [m["content"] for m in a.messages if m["role"] == "user"] == ["Eerste vraag.", "Tweede vraag."]
        await a.close()
    asyncio.run(run())


def test_interrupt_keeps_what_he_got_to_say():
    async def run():
        a = make_cascade()
        rec = Recorder(a)
        await a.start_session("p", "c", [])
        await say(a)
        await wait_for(lambda: "speaking" in rec.kinds())
        await a.interrupt()
        n = len(rec.audio)
        await asyncio.sleep(0.1)
        assert len(rec.audio) == n and "turn_complete" not in rec.kinds()
        assert a.messages[-1] == {"role": "assistant", "content": "Het is kwart voor twee. [onderbroken]"}
        await a.close()
    asyncio.run(run())


def test_the_next_brain_and_mouth_take_over():
    async def run():
        a = CascadeAdapter(ears=FakeEars(), brains=[FakeBrain(fail=True, model="first"), FakeBrain(model="second")],
                           mouths=[FakeMouth(fail=True), FakeMouth()], noise_filter=False)
        rec = Recorder(a)
        await a.start_session("p", "c", [])
        await say(a)
        await wait_for(lambda: "turn_complete" in rec.kinds())
        assert "error" not in rec.kinds()
        assert a.brains[0].model == "second" and not a.mouths[0].fail      # the working ones are first now
        assert list(a.usage) == ["second"]
        await a.close()
    asyncio.run(run())


def test_no_brain_at_all_is_an_error_not_silence():
    async def run():
        a = CascadeAdapter(ears=FakeEars(), brains=[FakeBrain(fail=True)], mouths=[FakeMouth()], noise_filter=False)
        rec = Recorder(a)
        await a.start_session("p", "c", [])
        await say(a)
        await wait_for(lambda: "error" in rec.kinds())
        assert a.is_open and [m["role"] for m in a.messages] == ["system", "user"]
        await a.close()
    asyncio.run(run())


def test_a_message_from_the_robot_is_answered_out_loud():
    async def run():
        a = make_cascade(script=[{"text": "De lijm is droog."}])
        rec = Recorder(a)
        await a.start_session("p", "c", [])
        await a.send_text("[timer] de lijm is droog")
        await wait_for(lambda: "turn_complete" in rec.kinds())
        assert a.messages[1] == {"role": "user", "content": "[timer] de lijm is droog"}
        assert "heard" not in rec.kinds() and rec.audio
        await a.close()
    asyncio.run(run())


def test_sounds_are_not_sentences():
    async def run():
        a = CascadeAdapter(ears=FakeEars(["Uh.", "...", "Hoe laat is het?"]), brains=[FakeBrain()], mouths=[FakeMouth()])
        rec = Recorder(a)
        await a.start_session("p", "c", [])
        await say(a)
        await say(a)
        assert "heard" not in rec.kinds() and rec.details("dropped") == ["Uh."]
        await say(a)
        await wait_for(lambda: "turn_complete" in rec.kinds())
        await a.close()
    asyncio.run(run())


def test_old_turns_go_in_one_step():
    a = make_cascade()
    a.messages = [{"role": "system", "content": "s"}]
    for n in range(cascade.MAX_TURNS + 1):
        a.messages += [{"role": "user", "content": f"q{n}"}, {"role": "assistant", "content": f"a{n}"}]
    a._trim()
    users = [m["content"] for m in a.messages if m["role"] == "user"]
    assert a.messages[0]["role"] == "system" and a.messages[1]["role"] == "user"
    assert len(users) == cascade.MAX_TURNS - 8 and users[-1] == f"q{cascade.MAX_TURNS}"


def test_three_bills_reach_the_meter(monkeypatch):
    got = []
    monkeypatch.setattr(usage, "HOOK", got.append)

    async def run():
        a = make_cascade()
        a.ears.usage = usage.add({}, {"promptTokensDetails": [{"modality": "AUDIO", "tokenCount": 30}]})
        rec = Recorder(a)
        await a.start_session("p", "c", [])
        await say(a)
        await wait_for(lambda: "turn_complete" in rec.kinds())
        await a.close()
    asyncio.run(run())
    assert {(r["source"], r["model"]) for r in got} == {("robot", "fake-llm"), ("robot", "fake-stt")}
    assert next(r for r in got if r["model"] == "fake-llm")["prompt_text"] == 100


# ---------------------------------------------------------------- sentences
def test_sentences_are_cut_where_a_voice_would_breathe():
    s = Sentences()
    out = s.feed("Dat kan. Je hebt bijv. 3.3 volt nodig, en de ESP32 is **niet** 5 volt-tolerant! Dus: 1. koop ")
    assert out == ["Dat kan.", "Je hebt bijv. 3.3 volt nodig, en de ESP32 is niet 5 volt-tolerant!"]
    assert s.feed("een level shifter") == [] and s.flush() == ["Dus: 1. koop een level shifter"]


def test_a_short_sentence_waits_for_the_next_except_the_first():
    s = Sentences(first=False)
    assert s.feed("Ja. ") == [] and s.feed("Dat is zo, want het regent. ") == ["Ja. Dat is zo, want het regent."]


def test_a_long_first_sentence_starts_at_a_comma():
    s = Sentences()
    text = "Een level shifter is nodig omdat de encoders op vijf volt werken, terwijl de ingangen van de ESP32 maar drie komma drie volt"
    out = s.feed(text)
    assert out == ["Een level shifter is nodig omdat de encoders op vijf volt werken,"]


def test_markup_and_links_are_not_read_out():
    assert cascade.clean("## Kijk *hier*: [de site](https://x.nl) 😀 `code`") == "Kijk hier: de site code"


def test_specs():
    assert cascade.parse_spec(" gemini:gemini-3.5-flash@minimal ") == ("gemini", "gemini-3.5-flash", "minimal")
    assert cascade.parse_spec("gemini") == ("gemini", "", "")
    assert cascade.chain_specs("a:b, c:d,") == ["a:b", "c:d"]
    with pytest.raises(ValueError):
        Brain("nobody:model")
    with pytest.raises(RuntimeError):
        CascadeAdapter(ears=FakeEars(), llm="nobody:model", mouths=[FakeMouth()])


def test_tool_calls_in_pieces_and_whole():
    calls = []
    for piece in ({"index": 0, "id": "c1", "function": {"name": "toon", "arguments": ""}},
                  {"index": 0, "function": {"arguments": '{"tekst":'}}, {"index": 0, "function": {"arguments": '"42"}'}},
                  {"index": 1, "id": "c2", "function": {"name": "kijk", "arguments": "{}"}}):
        cascade._merge_call(calls, piece)
    assert [(c["id"], c["function"]["name"], c["function"]["arguments"]) for c in calls] == \
        [("c1", "toon", '{"tekst":"42"}'), ("c2", "kijk", "{}")]
    calls = []                       # Gemini: whole calls, all under index 0, with a signature to hand back
    for piece in ({"index": 0, "id": "a", "function": {"name": "x", "arguments": "{}"}, "extra_content": {"google": {"thought_signature": "s"}}},
                  {"index": 0, "id": "b", "function": {"name": "y", "arguments": "{}"}}):
        cascade._merge_call(calls, piece)
    assert [c["id"] for c in calls] == ["a", "b"] and calls[0]["extra_content"]["google"]["thought_signature"] == "s"


# ---------------------------------------------------------------- the real parts against local servers
class Api:
    """A local HTTP server; `answer(path, headers, body)` -> (status, bytes or list of SSE dicts)."""

    def __init__(self, answer):
        self.requests = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                api.requests.append((self.path, dict(self.headers), body))
                status, payload = answer(self.path, self.headers, body)
                if isinstance(payload, list):
                    payload = b"".join(b"data: " + (p if isinstance(p, bytes) else json.dumps(p).encode()) + b"\n\n" for p in payload)
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


async def collect(stream):
    return [item async for item in stream]


def test_brain_streams_text_calls_and_usage_and_falls_back_on_the_key():
    def answer(path, headers, body):
        if headers["Authorization"] == "Bearer free":
            return 429, json.dumps({"error": {"message": "quota"}}).encode()
        if body.get("reasoning_effort"):
            return 400, json.dumps([{"error": {"message": "Thinking level MINIMAL is not supported"}}]).encode()
        return 200, [{"choices": [{"delta": {"content": "Het is "}}]}, {"choices": [{"delta": {"content": "laat."}}]},
                     {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "toon", "arguments": "{}"}}]}}]},
                     {"choices": [], "usage": {"prompt_tokens": 1000, "completion_tokens": 7,
                                               "prompt_tokens_details": {"cached_tokens": 900}}}, b"[DONE]"]
    api = Api(answer)
    try:
        brain = Brain("gemini:some-model@minimal", base_url=api.url, keys=["free", "paid"])
        tool = {"name": "toon", "description": "d", "parameters": {"type": "object", "properties": {}}}
        for _ in range(2):                           # the second time on the kept connection, straight to the paid key
            items = asyncio.run(collect(brain.stream([{"role": "user", "content": "hoi"}], [tool])))
            assert [v for k, v in items if k == "text"] == ["Het is ", "laat."]
            assert dict(items)["calls"][0]["function"]["name"] == "toon"
            # 100 fresh + 900 cached at a tenth
            assert usage.tokens(dict(items)["usage"])["prompt_text"] == 190
        assert len(api.requests) == 4 and brain.effort == "" and brain._key == 1
        path, _, body = api.requests[-1]
        assert path == "/v1/chat/completions" and body["stream"] and "reasoning_effort" not in body
        assert body["tools"] == [{"type": "function", "function": tool}]
        with pytest.raises(StageError):
            asyncio.run(collect(Brain("openai:m", base_url=api.url, keys=["free"]).stream([], [])))
    finally:
        api.close()


def test_mouth_streams_whole_samples():
    pcm = tone(300, 24_000)

    def answer(path, headers, body):
        return (200, pcm) if body["input"] != "stuk" else (500, b'{"error": {"message": "overloaded"}}')
    api = Api(answer)
    try:
        mouth = Mouth("openai:gpt-4o-mini-tts", "ballad", "Brisk.", 1.1, base_url=api.url, keys=["k"])
        out = asyncio.run(collect(mouth.stream("Hallo daar.")))
        assert b"".join(out) == pcm and all(len(chunk) % 2 == 0 for chunk in out)
        path, _, body = api.requests[0]
        assert path == "/v1/audio/speech" and body["response_format"] == "pcm" and body["voice"] == "ballad"
        assert body["instructions"].startswith("Brisk.") and "speed" not in body
        spoken = usage.tokens(mouth.meter())
        assert spoken["out_audio"] == round(0.3 * cascade.TTS_TOKENS_PER_S) and mouth.meter() == {}
        with pytest.raises(StageError):
            asyncio.run(collect(mouth.stream("stuk")))
    finally:
        api.close()


def test_ears_speak_the_transcription_session():
    async def run():
        seen = {"audio": 0}

        async def server(ws):
            seen["setup"] = json.loads(await ws.recv())
            await ws.send(json.dumps({"type": "session.updated"}))
            async for raw in ws:
                message = json.loads(raw)
                seen["audio"] += len(base64.b64decode(message["audio"]))
                if seen["audio"] >= 4800 * 3:
                    await ws.send(json.dumps({"type": "input_audio_buffer.speech_started"}))
                    await ws.send(json.dumps({"type": "input_audio_buffer.speech_stopped"}))
                    await ws.send(json.dumps({
                        "type": "conversation.item.input_audio_transcription.completed", "transcript": " Zet Radio 2 aan. ",
                        "usage": {"type": "tokens", "input_tokens": 25, "output_tokens": 9,
                                  "input_token_details": {"text_tokens": 1, "audio_tokens": 24}}}))
                    return
        async with websockets.serve(server, "127.0.0.1", 0) as mock:
            events = []

            async def on_event(kind, detail):
                events.append((kind, detail))
            ears = Ears(language="nl_en", hint="Willie", api_key="k", url=f"ws://127.0.0.1:{mock.sockets[0].getsockname()[1]}")
            await ears.open(on_event)
            for _ in range(3):
                await ears.send(SPEECH)              # 100 ms at 16 kHz -> 4800 bytes at 24 kHz
            await wait_for(lambda: ("closed", "speech-to-text closed the session") in events)
            assert events[:3] == [("speech_started", ""), ("speech_stopped", ""), ("text", "Zet Radio 2 aan.")]
            session = seen["setup"]["session"]
            assert session["type"] == "transcription"
            audio = session["audio"]["input"]
            assert audio["turn_detection"]["silence_duration_ms"] == 300
            assert audio["transcription"] == {"model": "gpt-4o-mini-transcribe", "language": "nl", "prompt": "Willie"}
            assert audio["format"] == {"type": "audio/pcm", "rate": 24000} and "noise_reduction" not in audio
            assert (ears.usage["prompt_audio"], ears.usage["prompt_text"], ears.usage["out_text"]) == (24, 1, 9)
            with pytest.raises(ConnectionError):
                await ears.send(SPEECH)
            await ears.close()
    asyncio.run(run())


def test_ears_guess_from_the_live_session_and_take_the_first_finished_text():
    async def run():
        first_setup, live_setup, live_messages = {}, {}, []
        release = asyncio.Event()

        async def first(ws):                          # turn detection + the slow text
            first_setup.update(json.loads(await ws.recv()))
            await ws.send(json.dumps({"type": "session.updated"}))
            got = 0
            async for raw in ws:
                got += 1
                if got == 1:
                    await ws.send(json.dumps({"type": "input_audio_buffer.speech_started"}))
                if got == 3:
                    await asyncio.sleep(0.05)         # the live session has written along by now
                    await ws.send(json.dumps({"type": "input_audio_buffer.speech_stopped"}))
                    await ws.send(json.dumps({"type": "input_audio_buffer.committed", "item_id": "a1"}))
                    await release.wait()
                    await ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.completed",
                                              "item_id": "a1", "transcript": "Zet Radio 2 aan."}))

        async def live(ws):                           # writes along, no turn detection
            live_setup.update(json.loads(await ws.recv()))
            await ws.send(json.dumps({"type": "session.updated"}))
            async for raw in ws:
                message = json.loads(raw)
                live_messages.append(message["type"])
                if live_messages.count("input_audio_buffer.append") == 2 and message["type"].endswith("append"):
                    for delta in ("zet radio ", "twee aan"):
                        await ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.delta", "delta": delta}))
                if message["type"] == "input_audio_buffer.commit":
                    await ws.send(json.dumps({"type": "input_audio_buffer.committed", "item_id": "b1"}))
                    await ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.completed",
                                              "item_id": "b1", "transcript": "Zet radio twee aan."}))
                    release.set()
        async with websockets.serve(first, "127.0.0.1", 0) as one, websockets.serve(live, "127.0.0.1", 0) as two:
            events = []

            async def on_event(kind, detail):
                events.append((kind, detail))
            ears = Ears(api_key="k", fast=True, url=f"ws://127.0.0.1:{one.sockets[0].getsockname()[1]}",
                        live_url=f"ws://127.0.0.1:{two.sockets[0].getsockname()[1]}")
            await ears.open(on_event)
            for _ in range(3):
                await ears.send(SPEECH)
            await wait_for(lambda: ("text", "Zet radio twee aan.") in events)
            await asyncio.sleep(0.1)                  # the first session's later text is the same sentence: not twice
            assert events == [("speech_started", ""), ("speech_stopped", ""), ("guess", "zet radio twee aan"),
                              ("text", "Zet radio twee aan.")]
            assert first_setup["session"]["audio"]["input"]["turn_detection"]["type"] == "server_vad"
            live_audio = live_setup["session"]["audio"]["input"]
            assert live_audio["turn_detection"] is None
            assert live_audio["transcription"] == {"model": "gpt-live-transcribe", "delay": "minimal", "languages": ["nl"]}
            assert usage.tokens(ears.live_meter())["prompt_audio"] == 3 and ears.live_meter() == {}
            await ears.close()
    asyncio.run(run())


def test_prices_of_the_three_parts():
    assert usage.family("gpt-live-transcribe") == "transcribe-live"
    # A minute of open mic on the live session: 600 audio tokens = OpenAI's $0.017.
    assert round(usage.usd("gpt-live-transcribe", {"prompt_audio": 600}), 3) == 0.017
    assert usage.family("gpt-4o-mini-transcribe") == "transcribe-mini" and usage.family("gpt-4o-transcribe") == "transcribe"
    assert usage.family("gpt-4o-mini-tts") == "gpt-tts" and usage.family("gemini-3.8-flash-tts") == "tts"
    assert usage.family("gpt-5.4-mini") == "gpt-mini" and usage.family("gpt-4.1-mini") == "gpt-4.1-mini"
    assert usage.family("gpt-realtime-2.1-mini") == "realtime-mini"
    # A minute of his voice: 60 s x 21 audio tokens at $12 per 1M = OpenAI's $0.015.
    assert round(usage.usd("gpt-4o-mini-tts", {"out_audio": 60 * cascade.TTS_TOKENS_PER_S}), 3) == 0.015
