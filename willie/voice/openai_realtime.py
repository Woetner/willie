"""Voice adapter B (D5): OpenAI Realtime on the D3 interface.

Protocol only, no sound card: `gemini_live.session()` runs it on the Pi when
`voice.adapter: openai_realtime` (willie.yaml). Checked against OpenAI's docs on 9 Oct:

  - wss://api.openai.com/v1/realtime?model=..., key in the Authorization header
  - PCM is 24 kHz only, both ways. The mic stays at 16 kHz (the wake word shares it), so the
    uplink is resampled here.
  - no built-in web search: `search` is always False, so he searches through zoek_op
  - no session resumption: `resume` is accepted and ignored (a garage conversation starts
    fresh after a reconnect; one session lasts 60 minutes at most)
  - a client-side cancel exists (`response.cancel`), so interrupt() really stops him
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os

import websockets

import willie.voice as voice_keys
from willie import usage
from willie.voice.base import Tool, VoiceAdapter

log = logging.getLogger("willie.voice.openai")

URL = "wss://api.openai.com/v1/realtime"
# Newest first; the next one is tried when a model is refused.
MODELS = ("gpt-realtime-2.1", "gpt-realtime-2", "gpt-realtime")
IN_RATE, API_RATE = 16_000, 24_000
VOICES = ("alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar")
VOICE = "cedar"              # voice.openai_voice in willie.yaml overrides it
TRANSCRIBE = "gpt-4o-mini-transcribe"
# The same rule as the Gemini adapter: the language of the transcription alone does not stop
# the model answering an English word in English.
LANGUAGES = {
    "nl": "Spreek altijd Nederlands. Alleen als Wouter zelf Engels praat, antwoord je in het Engels.",
    "en": "Always speak English.",
    # Wouter, 9 Oct: Dutch in, English out - the preset voices are English voices.
    "nl_en": ("Wouter speaks Dutch to you. You ALWAYS answer in English, spoken and on the screen, also when the "
              "text above, a tool result or his question is Dutch: translate it. Tool names and their arguments "
              "stay as they are defined."),
}
# Measured on gpt-realtime-2.1-mini (G1 sheet, 9 Oct): it looked up the capital of Mongolia with five
# tools, announced every tool before calling it and said "it is on your screen" without toon.
DISCIPLINE = (
    "Tool rules. Answer general knowledge, explanations and arithmetic directly from what you know: no lookup. "
    "Look something up only when the question needs the robot, the house, Wouter's own data, the camera "
    "or something recent (opening hours, prices, news, weather). One lookup per question; never chain "
    "lookups for a simple fact. Do not announce a tool before calling it. Never say something is on the "
    "screen unless you called toon, kaart, toon_afbeelding or pinout in this turn."
)
# Wouter, 9 Oct: "prefer to put things on his screen visually ... his voice helps me understand what
# is on the screen. Don't make him talk more than he needs to."
SCREEN = (
    "Screen first. You have a screen; Wouter looks at it while you talk. Whenever an answer contains "
    "facts he can read (times, prices, numbers, a list, a comparison, steps), put it on the screen with "
    "one kaart call BEFORE you speak. After a lookup the order is: lookup, kaart, then speak. "
    "Pick the most visual kind: "
    "values over time (prices over weeks or months, weather per hour, temperature, usage) = grafiek; "
    "several amounts side by side (Euro 95 against diesel, shop against shop) = staaf; "
    "a reading on a scale (battery, air quality, humidity) = meter; "
    "one key value = getal, with the value large and the supporting lines under it "
    "(shop hours: waarde 'tot 21:00', regels = the hours per day of the week); "
    "two options = vergelijk; a how-to = stappen; otherwise punten. "
    "How something looks = toon_afbeelding. A single bare number with nothing around it = toon. "
    "Put real numbers with units on the card, only from the lookup or what you know for certain; "
    "if the lookup gave one value and no history, show getal or staaf, do not invent a graph. "
    "If the result holds values for several dates or months, it MUST be a grafiek with those points. "
    "For a price or a rate, ask the lookup for today's value and the last months in one question. "
    "One screen call per answer: kaart or toon, never both. "
    "Then speak one or two short sentences: the answer itself first, then what to look at on the screen "
    "('Open until nine tonight. The whole week is on my screen.'). Never read the card aloud, never "
    "repeat what is on it line by line, no closing offers such as 'want more details?'. "
    "A command gets one or two words ('Done.'). "
    "Say nothing before or between tool calls, in any language: no 'Even kijken', no 'Let me check'. "
    "Your first words come after the last tool result.\n"
    "Example 1. Wouter: 'Tot hoe laat is de Action open?' -> zoek_op, then "
    'kaart {"soort": "getal", "titel": "Action - open today", "waarde": "until 21:00", '
    '"regels": ["Mon-Fri 08:30-21:00", "Sat 08:30-20:00", "Sun 10:00-18:00"]}, then say: '
    "'Open until nine tonight. The whole week is on my screen.'\n"
    "Example 2. Wouter: 'Wat is de actuele benzineprijs?' and the lookup gives today's price and earlier months -> "
    'kaart {"soort": "grafiek", "titel": "Euro 95 per litre", "eenheid": "EUR", "vorm": "lijn", '
    '"punten": [{"naam": "May", "waarde": 2.05}, {"naam": "Jun", "waarde": 2.09}, {"naam": "Oct", "waarde": 2.19}]}, '
    "then say: 'Two nineteen a litre today. The graph shows it climbing since May.'"
)
# Tool results are Dutch (the hub's "zeg" lines, orders such as "Zeg 'Onthouden.'") and he read
# them out as they came (Wouter, 9 Oct: "Dutch words slip in with tool use"). The reminder rides
# on every result, where a small model still sees it.
ENGLISH_RESULT = ("This result is Dutch data. Speak English only: translate anything you say from it, "
                  "also a word or sentence it tells you to say.")
HEARS = {"nl": "nl", "en": "en", "nl_en": "nl"}      # the language of his words, for the transcription
# Cached input costs $0.40 per 1M for text and audio alike; the meter has no cached column,
# so cached tokens are counted as this share of a text token ($4.00). The cost is right, the
# text token count in the meter is a little low.
CACHED_AS_TEXT = 0.1
# gpt-realtime-2.1-mini called zaklamp 13 times in a row on one question (9 Oct). After this many
# rounds of tool calls without a word from Wouter the next answer must be spoken: no more tools.
MAX_TOOL_ROUNDS = 3
# "Rate limit reached ... tokens per min (TPM): Limit 200000 ... try again in 1.185s" (9 Oct): every
# response re-reads the 16k setup, so about twelve responses a minute fit. A refused one left
# him silent; it is asked again after a short wait, at most this often per turn.
LIMIT_RETRIES, LIMIT_WAIT_S = 3, 2.0


def to_meter(used: dict) -> dict:
    """`response.done` usage -> the usageMetadata shape willie/usage.py reads."""
    into = used.get("input_token_details") or {}
    cached = into.get("cached_tokens_details") or {}
    text = int(into.get("text_tokens") or 0) - int(cached.get("text_tokens") or 0)
    audio = int(into.get("audio_tokens") or 0) - int(cached.get("audio_tokens") or 0)
    image = int(into.get("image_tokens") or 0) - int(cached.get("image_tokens") or 0)
    text += round(int(into.get("cached_tokens") or 0) * CACHED_AS_TEXT)
    out = used.get("output_token_details") or {}
    return {
        "promptTokensDetails": [{"modality": "TEXT", "tokenCount": max(0, text)},
                                {"modality": "AUDIO", "tokenCount": max(0, audio)},
                                {"modality": "IMAGE", "tokenCount": max(0, image)}],
        "responseTokensDetails": [{"modality": "TEXT", "tokenCount": int(out.get("text_tokens") or 0)},
                                  {"modality": "AUDIO", "tokenCount": int(out.get("audio_tokens") or 0)}],
    }


def upsample(pcm: bytes, last: int = 0) -> tuple[bytes, int]:
    """16 kHz -> 24 kHz, straight lines between samples. `last` is the final sample of the
    previous chunk; the new one is returned with the sound, so chunks join without a click."""
    import numpy as np
    samples = np.frombuffer(pcm[: len(pcm) // 2 * 2], "<i2")
    if not len(samples):
        return b"", last
    known = np.concatenate(([last], samples)).astype(np.float32)
    at = (np.arange(len(samples) * API_RATE // IN_RATE) + 1) * (IN_RATE / API_RATE)
    return np.interp(at, np.arange(len(known)), known).astype("<i2").tobytes(), int(samples[-1])


class OpenAIRealtimeAdapter(VoiceAdapter):
    name = "openai_realtime"
    in_rate = IN_RATE
    out_rate = API_RATE

    def __init__(self, api_key: str | None = None, model: str = "", url: str | None = None,
                 setup_timeout: float = 15.0, language: str = "nl", voice: str = "", speed: float = 1.0,
                 search: bool = False, resume: bool = False, resume_handle: str = "",
                 source: str = "robot"):
        """`model` = empty: try MODELS in order. `url` replaces the OpenAI endpoint - the tests
        point it at a local mock server. `search`, `resume` and `resume_handle` are taken so
        the runner can build either adapter the same way; see the module text."""
        super().__init__()
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.models = [model] if model else list(MODELS)
        self.voice = voice if voice in VOICES else VOICE
        self.speed = min(1.5, max(0.25, float(speed or 1.0)))
        self.language = language
        self.search = False
        self.resume, self.resume_handle = False, ""
        self.url = url
        self.setup_timeout = setup_timeout
        self.source = source
        self.model = ""
        self.key_kind = "betaald"        # OpenAI has no free tier for this
        self.usage: dict = {}
        self._socket = None
        self._receiver: asyncio.Task | None = None
        self._speaking = False           # "speaking" already sent for the current model turn
        self._dropping = False           # interrupt() was called: discard the rest of this turn
        self._closing = False
        self._last = 0                   # last mic sample of the previous chunk (resampler)
        self._rounds = 0                 # tool rounds since the last answer without one
        self._limited = 0                # rate-limit retries in this turn
        self._retry: asyncio.Task | None = None

    # ---- lifecycle -------------------------------------------------------------
    async def start_session(self, persona: str, context: str, tools: list[Tool]) -> None:
        if not self.api_key and not self.url:
            raise RuntimeError("OPENAI_API_KEY missing")
        self._register_tools(tools)
        # The persona and its examples are Dutch and pull him back to Dutch (2 of 7 answers on
        # 9 Oct), so English-out is said before them and again after everything.
        first = "OUTPUT LANGUAGE: English only. The Dutch examples below show the manner, not the language." \
            if self.language == "nl_en" else ""
        prompt = "\n\n".join(p for p in (first, persona, DISCIPLINE, SCREEN, context, LANGUAGES.get(self.language, "")) if p).strip()
        last_error = "no model accepted the session"
        for model in self.models:
            url = self.url or f"{URL}?model={model}"
            try:
                socket = await websockets.connect(url, max_size=None, ping_interval=20,
                                                  additional_headers={"Authorization": f"Bearer {self.api_key}"})
            except (websockets.WebSocketException, OSError) as exc:
                last_error = f"{model}: {exc}"
                continue
            try:
                await socket.send(json.dumps(self._setup_message(model, prompt)))
                refused = await asyncio.wait_for(self._await_setup(socket), timeout=self.setup_timeout)
            except (asyncio.TimeoutError, websockets.WebSocketException, OSError) as exc:
                refused = str(exc) or "no setup answer"
            if refused:
                last_error = f"{model}: {refused[:160]}"
                await socket.close()
                continue
            self._socket, self.model = socket, model
            voice_keys.SESSION.update(model=model, key=self.key_kind)
            if voice_keys.KEY_HOOK:
                try:
                    voice_keys.KEY_HOOK(True)
                except Exception:
                    pass
            self.is_open, self._closing = True, False
            self._receiver = asyncio.create_task(self._receive())
            await self._emit("ready", model)
            return
        raise RuntimeError(last_error)

    @staticmethod
    async def _await_setup(socket) -> str:
        """Empty when the server took the settings, else its error text."""
        async for raw in socket:
            message = json.loads(raw)
            if message.get("type") == "session.updated":
                return ""
            if message.get("type") == "error":
                return (message.get("error") or {}).get("message") or json.dumps(message)
        return "server closed the session"

    def _setup_message(self, model: str, prompt: str) -> dict:
        transcription = {"model": TRANSCRIBE}
        if self.language in HEARS:
            transcription["language"] = HEARS[self.language]
        session = {
            "type": "realtime",
            "output_modalities": ["audio"],
            # A model does not know its own name or version, so say it (as the Gemini adapter does).
            "instructions": f"{prompt}\n\nJe draait op het model {model}.",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": API_RATE},
                    # Text of both sides: the face, the follow-up window and his memory run on it.
                    "transcription": transcription,
                    # The server decides from the words whether the sentence is over; "high"
                    # answers soonest (it waits 2 s at most).
                    "turn_detection": {"type": "semantic_vad", "eagerness": "high",
                                       "create_response": True, "interrupt_response": True},
                },
                "output": {"format": {"type": "audio/pcm", "rate": API_RATE},
                           "voice": self.voice, "speed": self.speed},
            },
            "tools": [{"type": "function", **t.declaration()} for t in self.tools.values()],
            "tool_choice": "auto",
        }
        return {"type": "session.update", "session": session}

    # ---- uplink ----------------------------------------------------------------
    async def _send(self, message: dict) -> None:
        if not self.is_open or self._socket is None:
            raise ConnectionError("session closed")
        try:
            await self._socket.send(json.dumps(message))
        except websockets.ConnectionClosed as exc:
            raise ConnectionError(f"session closed: {exc}") from exc

    async def send_audio(self, pcm: bytes) -> None:
        audio, self._last = upsample(pcm, self._last)
        audio = base64.b64encode(audio).decode("ascii")
        await self._send({"type": "input_audio_buffer.append", "audio": audio})

    async def send_text(self, text: str) -> None:
        """A message from the robot itself (a timer, a danger he saw) as a user turn: the
        model answers it out loud in the same conversation."""
        item = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}
        await self._send({"type": "conversation.item.create", "item": item})
        await self._send({"type": "response.create"})

    async def interrupt(self) -> None:
        if self._speaking:
            self._speaking = False
            self._dropping = True                    # until this response's response.done
            try:
                await self._send({"type": "response.cancel"})
            except ConnectionError:
                pass
            await self._emit("interrupted")

    async def close(self) -> None:
        self._report_usage()
        voice_keys.SESSION.update(model=None, key=None)
        if voice_keys.KEY_HOOK:
            try:
                voice_keys.KEY_HOOK(False)
            except Exception:
                pass
        if not self.is_open and self._socket is None:
            return
        self._closing = True
        self.is_open = False
        if self._socket is not None:
            await self._socket.close()
        if self._receiver is not None and self._receiver is not asyncio.current_task():
            self._receiver.cancel()
            await asyncio.gather(self._receiver, return_exceptions=True)
        self._socket = self._receiver = None
        await self._emit("closed", "close()")

    # ---- downlink --------------------------------------------------------------
    async def _receive(self) -> None:
        reason = "server closed the session"
        try:
            async for raw in self._socket:
                await self._handle(json.loads(raw))
        except websockets.ConnectionClosed as exc:
            reason = f"connection lost: {exc}"
        except Exception as exc:                     # a bad message must be visible, not silent
            log.exception("openai receive failed")
            reason = f"{type(exc).__name__}: {exc}"
            await self._emit("error", reason)
        if not self._closing:                        # the server ended it, not close()
            self.is_open = False
            self._socket = None
            self._report_usage()
            await self._emit("closed", reason)

    def _report_usage(self) -> None:
        """Once per session, when it ends (either side): the summed tokens to the meter."""
        if self.usage:
            usage.report(self.source, self.model, self.key_kind, self.usage)
            self.usage = {}

    async def _handle(self, message: dict) -> None:
        kind = message.get("type", "")
        if kind in ("response.output_audio.delta", "response.audio.delta"):
            if message.get("delta") and not self._dropping:
                if not self._speaking:
                    self._speaking = True
                    await self._emit("speaking")
                await self._deliver_audio(base64.b64decode(message["delta"]))
        elif kind in ("response.output_audio_transcript.delta", "response.audio_transcript.delta"):
            if message.get("delta") and not self._dropping:
                await self._emit("said", message["delta"])
        elif kind == "conversation.item.input_audio_transcription.completed":
            if (message.get("transcript") or "").strip():
                await self._emit("heard", message["transcript"])
        elif kind == "input_audio_buffer.speech_started":
            if self._speaking:                       # the user talked over him (server VAD)
                self._speaking = False
                await self._emit("interrupted")
        elif kind == "response.done":
            response = message.get("response") or {}
            if response.get("usage"):
                usage.add(self.usage, to_meter(response["usage"]))
            calls = [item for item in response.get("output") or [] if item.get("type") == "function_call"]
            if calls:
                # As its own task: the camera takes seconds and the audio must keep flowing.
                asyncio.create_task(self._answer_tools(calls))
            elif self._dropping or response.get("status") == "cancelled":
                pass                                 # "interrupted" was already sent
            elif response.get("status") == "failed":
                error = ((response.get("status_details") or {}).get("error") or {}).get("message") or "response failed"
                if not self._again(error):
                    await self._emit("error", error)
            else:
                self._limited = 0
                await self._emit("turn_complete")
            if not calls:
                self._rounds = 0
            self._speaking = self._dropping = False
        elif kind == "error":
            error = (message.get("error") or {}).get("message") or json.dumps(message)[:200]
            if not self._again(error):
                await self._emit("error", error)

    def _again(self, error: str) -> bool:
        """A response refused for the rate limit: ask once more after a wait. True = it will be."""
        if "rate limit" not in error.lower() or self._limited >= LIMIT_RETRIES:
            return False
        if self._retry is None or self._retry.done():
            self._limited += 1
            log.warning("rate limit, retry %d in %.0f s: %s", self._limited, LIMIT_WAIT_S, error[:120])

            async def later():
                await asyncio.sleep(LIMIT_WAIT_S)
                try:
                    await self._send({"type": "response.create"})
                except ConnectionError:
                    pass
            self._retry = asyncio.create_task(later())
        return True

    async def _answer_tools(self, calls: list[dict]) -> None:
        """Run the calls of one response, hand back the results, then ask him to go on."""
        async def run(call: dict) -> dict:
            try:
                args = json.loads(call.get("arguments") or "{}")
            except ValueError:
                args = {}
            result = await self._run_tool(call.get("name", ""), args)
            if self.language == "nl_en":
                result = {**result, "language": ENGLISH_RESULT} if isinstance(result, dict) \
                    else {"result": result, "language": ENGLISH_RESULT}
            return {"type": "function_call_output", "call_id": call.get("call_id"),
                    "output": json.dumps(result, ensure_ascii=False, default=str)}
        try:
            for item in await asyncio.gather(*(run(call) for call in calls)):
                await self._send({"type": "conversation.item.create", "item": item})
            self._rounds += 1
            if self._rounds >= MAX_TOOL_ROUNDS:
                log.warning("tool loop: %d rounds, last %s - answer without tools now", self._rounds,
                            [call.get("name") for call in calls])
                await self._send({"type": "response.create", "response": {"tool_choice": "none"}})
            else:
                await self._send({"type": "response.create"})
        except ConnectionError:
            pass
