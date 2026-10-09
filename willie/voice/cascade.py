"""Voice adapter C: the cascade - speech-to-text, a text model, text-to-speech - on the D3 interface.

Why (Wouter, 9 Oct): OpenAI Realtime costs too much and Gemini Live does not talk well enough.
A text model is cheaper per token than a speech-to-speech one, its prompt is cached instead of
paid again on every turn, any model can be the brain, and each of the three parts can be
swapped or bought elsewhere without touching the other two.

    mic 16 kHz -> Ears  (streaming speech-to-text; the server decides when the sentence is over)
               -> Brain (a text model with the tools, streamed; any OpenAI-compatible endpoint)
               -> Mouth (text-to-speech per sentence, streamed) -> on_audio at 24 kHz

Protocol only, no sound card: `gemini_live.session()` runs it on the Pi when
`voice.adapter: cascade`, so wake word, face, echo gate, tools, memory and the follow-up
window are the same as with the other two adapters. No new package: the websocket library
the Live adapters use, and the standard library's HTTP client in a worker thread.

What makes it quick enough to talk to (measured in WILL-E.md §12):
  - the brain starts on what a second, live speech-to-text session wrote while Wouter talked,
    half a second after his last word; the finished text confirms it before anything is heard
    or done (voice.stt_fast; without it the text takes 1.2 s)
  - the answer is spoken per sentence: the first sentence goes to the speech engine while the
    model is still writing the second
  - the HTTPS connections to the brain and the mouth are opened when the session starts and
    kept open between turns (a TLS handshake costs the Pi 3 a few hundred ms)

Chains (`voice.llm`, `voice.tts`): a comma-separated list, first one that works. When a part
fails before it gave anything, the next takes that turn and stays first for the session.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import http.client
import json
import logging
import os
import re
import socket
import threading
import time
from urllib.parse import urlsplit

import websockets

import willie.voice as voice_keys
from willie import usage
from willie.voice.base import Tool, VoiceAdapter
from willie.voice.openai_realtime import API_RATE, DISCIPLINE, ENGLISH_RESULT, HEARS, LANGUAGES, SCREEN, upsample

log = logging.getLogger("willie.voice.cascade")

# ---- defaults (willie.yaml: voice.stt_model, voice.llm, voice.tts) ---------------------------
STT_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
STT_MODEL = "gpt-4o-mini-transcribe"
STT_LIVE = "gpt-live-transcribe"
# When a sentence is over: 300 ms of silence. Measured with the G1 questions (end of the sound ->
# "stopped"): this setting about 0.5 s, 500 ms 0.76 s, the server's reading of the words
# (semantic_vad, eager) 0.75 s. A pause this short in the middle of a sentence is caught by
# the adapter: he starts over with the whole sentence as long as he has not made a sound.
TURN = {"type": "server_vad", "silence_duration_ms": 300, "threshold": 0.5, "prefix_padding_ms": 300}
# The brain, timed on the Pi with the real 34 kB prompt (9 Oct, first text after the question):
# gpt-5.4-mini 0.65-1.3 s, gpt-4.1-mini 0.5-0.9 s, gemini-3.5-flash 1.3 s, gemini-3.8-flash 3 s
# (it thinks first and cannot be told not to). The second one only speaks when the first fails.
LLM = "openai:gpt-5.4-mini@none, gemini:gemini-3.5-flash@minimal"
TTS = "openai:gpt-4o-mini-tts, gemini"
VOICE = "ballad"

# name -> (base URL of an OpenAI-compatible API, the environment names of its keys, tried in order).
# Gemini: the free key first, as for every other conversation (willie.voice.talk_keys).
PROVIDERS = {
    "openai": ("https://api.openai.com/v1", ("OPENAI_API_KEY",)),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", ("GEMINI_API_KEY_FREE", "GEMINI_API_KEY")),
    "groq": ("https://api.groq.com/openai/v1", ("GROQ_API_KEY",)),
    "deepseek": ("https://api.deepseek.com", ("DEEPSEEK_API_KEY",)),
    "cerebras": ("https://api.cerebras.ai/v1", ("CEREBRAS_API_KEY",)),
    "openrouter": ("https://openrouter.ai/api/v1", ("OPENROUTER_API_KEY",)),
    "local": ("", ()),          # voice.local_llm_url / voice.local_tts_url: Ollama, Kokoro-FastAPI, ... on the home server
}

SPOKEN = (
    "Your answer is read aloud by a speech engine, sentence by sentence. Write only what should be said: "
    "plain sentences, no markdown, lists, headings, emoji, brackets or web addresses. Write numbers, units "
    "and abbreviations the way they are said. Begin with a short first sentence, so the speaking can start at once."
)
# Talk less, show more (Wouter, 9 Oct). The brain says the answer in a sentence or two and writes
# the rest as notes after a [SCHERM] line; a second, cheaper model (voice.screen_llm) turns those
# notes into one kaart while he is already talking. That is sooner and cheaper than the brain
# calling kaart itself (a second round over the whole prompt before his first word), and what
# is not said is not paid as speech - the largest part of the bill.
MARK = "[SCHERM]"
SCREEN_LLM = "openai:gpt-5-mini@minimal, gemini:gemini-3.5-flash-lite@minimal"
TALK_LESS = (
    "Talk less, show more. You have a screen; Wouter reads it while you talk. Say at most two short sentences, "
    "about 25 words in all: the answer itself. A command gets one or two words ('Done.'). Everything else that "
    "helps him - the numbers, the steps, a list, a comparison, values over time, the reasons - you do NOT say: "
    f"write it after a line that holds only {MARK}, as short plain notes with the real numbers and units, only "
    "from a lookup or from what you know for certain. Those notes are never spoken: a screen assistant draws "
    "them as one card (graph, bars, meter, steps, comparison, key value). When there are notes, your last spoken "
    f"sentence may point at the screen. No {MARK} part for small talk, a done command, or an answer that is "
    "complete in one sentence. The notes are how a card gets on the screen: the tool kaart does not exist in "
    "this conversation, and wherever your instructions name it you write notes instead. toon is only for when "
    "Wouter asks, in those words, to put a given text or number on the screen; never for an explanation. "
    "How something looks = toon_afbeelding. Say nothing before or between tool calls, in any language; your "
    "first words come after the last tool result. No closing offers such as 'want more details?'.\n"
    "Example. Wouter: 'Waarom heb ik een level shifter nodig tussen 5 volt encoders en een ESP32?' -> "
    "'Because the ESP32's pins only take 3.3 volts. The details are on my screen.\n"
    f"{MARK}\nLevel shifter: 5 V encoder to ESP32\n- ESP32 GPIO: 3.3 V logic, 3.6 V absolute maximum\n"
    "- Encoder output: 5 V, damages the pin over time\n- Fix: level shifter or a 10k / 20k divider per channel'"
)
DRAW = (
    "You draw one card on a small robot's screen (480 x 320) from the notes you are given. You do not speak: "
    "answer with exactly one kaart call. Use only facts and numbers that are in the notes, never your own. "
    "Pick the most visual kind: values over time = grafiek; several amounts side by side = staaf; a reading on "
    "a scale = meter; one key value = getal, large, with the supporting lines under it; two options = vergelijk; "
    "a how-to = stappen; otherwise punten. Copy, do not write: no value you worked out yourself, no added advice, "
    "no sentences. At most 5 lines of at most 40 characters each, and a title of at most 30."
)
STYLES = {
    "nl": "Spreek Nederlands zoals een Nederlander. Vlot en zakelijk, in een stevig tempo, droog en rustig, zonder opgewekte ondertoon.",
    "en": "Brisk, dry and matter-of-fact, calm and precise, like a capable butler. No cheerful announcer tone.",
}
STYLES["nl_en"] = STYLES["en"]

MAX_ROUNDS = 6               # model -> tools -> model ... per question
MAX_TURNS = 24               # questions kept in the conversation; the oldest 8 go at once (keeps the cached prefix)
TOOL_CHARS = 6000            # of one tool result kept for the model
CONFIRM_S = 4.0              # an answer started on a guess waits this long for the finished text, then is dropped
QUIET_S = 3.0                # his first sound waits this long for Wouter to stop talking
CACHED_AS_TEXT = 0.1         # a cached prompt token costs about a tenth (the meter has no cached column)
# gpt-4o-mini-tts answers with sound only, no token count: OpenAI's own estimate is $0.015 a
# minute at $12 per 1M audio tokens = about 21 tokens a second.
TTS_TOKENS_PER_S = 21


class StageError(RuntimeError):
    """A part of the chain refused or failed; `status` is the HTTP status when there was one."""

    def __init__(self, text: str, status: int = 0):
        super().__init__(text)
        self.status = status


class Unconfirmed(Exception):
    """An answer started on a guess of the sentence, and the finished text never came."""


# ---- HTTP: one kept-open connection per part, read in a worker thread -------------------------
class Http:
    def __init__(self, base_url: str, timeout: float = 30.0):
        parts = urlsplit(base_url)
        self.secure, self.host, self.path = parts.scheme == "https", parts.netloc, parts.path.rstrip("/")
        self.timeout = timeout
        self._connection: http.client.HTTPConnection | None = None

    def _open(self) -> http.client.HTTPConnection:
        if self._connection is None:
            kind = http.client.HTTPSConnection if self.secure else http.client.HTTPConnection
            self._connection = kind(self.host, timeout=self.timeout)
        return self._connection

    def warm(self) -> None:
        """The TCP and TLS handshake now, so the first question does not wait for it."""
        try:
            connection = self._open()
            if connection.sock is None:
                connection.connect()
        except OSError:
            self.drop()

    def post(self, path: str, headers: dict, body: dict) -> http.client.HTTPResponse:
        payload = json.dumps(body, ensure_ascii=False).encode()
        for attempt in (0, 1):
            connection = self._open()
            try:
                connection.request("POST", self.path + path, payload, {"Content-Type": "application/json", **headers})
                return connection.getresponse()
            except (http.client.HTTPException, OSError) as exc:
                self.drop()                      # a kept connection the server closed meanwhile: once more, fresh
                if attempt:
                    raise StageError(f"{self.host}: {type(exc).__name__}: {exc}") from exc
        raise StageError(self.host)

    def drop(self) -> None:
        """Close the connection; a thread reading from it stops at once. Safe from any thread."""
        connection, self._connection = self._connection, None
        if connection is None:
            return
        try:
            if connection.sock is not None:
                connection.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass


async def in_thread(work, abort):
    """Run `work(put)` in a thread and yield what it puts. Its exception is raised here;
    leaving early (a cancel) calls `abort()`, which must make `work` return."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    end = object()

    def run() -> None:
        try:
            work(lambda item: loop.call_soon_threadsafe(queue.put_nowait, item))
            last = end
        except BaseException as exc:
            last = exc
        try:
            loop.call_soon_threadsafe(queue.put_nowait, last)
        except RuntimeError:                     # the loop is gone: nobody is listening
            pass

    threading.Thread(target=run, name="cascade", daemon=True).start()
    finished = False
    try:
        while True:
            item = await queue.get()
            if item is end:
                finished = True
                return
            if isinstance(item, BaseException):
                finished = True
                raise item
            yield item
    finally:
        if not finished:
            abort()


def _error_text(response: http.client.HTTPResponse) -> str:
    raw = response.read(2000).decode("utf-8", "replace")
    try:
        found = json.loads(raw)
        found = found[0] if isinstance(found, list) and found else found
        return str((found.get("error") or {}).get("message") or raw)[:300]
    except (ValueError, AttributeError):
        return raw[:300]


def parse_spec(spec: str) -> tuple[str, str, str]:
    """"gemini:gemini-3.8-flash@minimal" -> (provider, model, reasoning effort or "")."""
    spec = spec.strip()
    provider, _, model = spec.partition(":")
    model, _, effort = model.partition("@")
    return provider.strip(), model.strip(), effort.strip()


def chain_specs(text: str) -> list[str]:
    return [part.strip() for part in str(text or "").split(",") if part.strip()]


def _keys(provider: str) -> list[str]:
    names = PROVIDERS.get(provider, ("", ()))[1]
    return [key for key in dict.fromkeys(os.environ.get(name, "") for name in names) if key]


# ---- Ears: streaming speech-to-text with the turn detection on the server ---------------------
class Ears:
    """OpenAI's realtime transcription session: the sound goes up as it comes, the server marks
    where a sentence starts and stops and writes it out. Events to `on_event(kind, detail)`:
    speech_started, speech_stopped, guess (see below), text (one finished sentence), error, closed.

    Measured on the Pi (9 Oct, §12). Wouter's own clips: 6 of 6 sentences right. With the
    server's noise reduction ("far_field") the same clips gave "ChatGPT." and nothing, so it is
    not asked for: his mic already has its own cleaning. The slow part is the text: this model
    only starts writing when the sentence is over, so the text is there 1.2 s after the last word
    whatever the turn detection is set to.

    `fast` takes that second away with a second session next to the first: gpt-live-transcribe
    writes along while Wouter talks (it has no turn detection of its own, the first session
    keeps that job). When the sentence stops, what it wrote so far goes out as `guess`, so the
    brain can start at once; the finished text of whichever session is first follows as `text`.
    The adapter only lets the answer be heard, and tools run, once `text` says the guess was right.
    """

    def __init__(self, model: str = "", language: str = "nl", hint: str = "", api_key: str | None = None,
                 url: str | None = None, setup_timeout: float = 10.0, fast: bool = False, live_url: str | None = None):
        self.model = model or STT_MODEL
        self.language, self.hint = HEARS.get(language, ""), hint
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.url, self.setup_timeout = url, setup_timeout
        self.fast, self.live_url = fast, live_url
        self.usage: dict = {}
        self.live_seconds = 0.0      # sound sent to the live session, for the meter
        self._socket = self._live = None
        self._tasks: list[asyncio.Task] = []
        self._event = None
        self._last = 0               # last mic sample of the previous chunk (resampler)
        self._closing = False
        self._partial = ""           # what the live session wrote of the sentence being said
        self._talking = False        # between the first session's "started" and "stopped"
        self._recent: collections.deque = collections.deque(maxlen=12)       # the last ~1 s of sound, not yet sent live
        self._turn = self._done = 0  # sentences ended / sentences whose text went out
        self._items: dict[str, int] = {}             # the server's item id -> which sentence
        self._empty = 0              # a sentence one session found no words in

    def _setup_message(self, live: bool = False) -> dict:
        if live:
            transcription = {"model": STT_LIVE, "delay": "minimal"}
            if self.language:
                transcription["languages"] = [self.language]
        else:
            transcription = {"model": self.model}
            if self.language:
                transcription["language"] = self.language
        if self.hint:
            transcription["prompt"] = self.hint
        return {"type": "session.update", "session": {"type": "transcription", "audio": {"input": {
            "format": {"type": "audio/pcm", "rate": API_RATE},
            "transcription": transcription,
            "turn_detection": None if live else TURN,
        }}}}

    async def _connect(self, url: str, setup: dict, name: str):
        socket_ = None
        try:
            socket_ = await websockets.connect(url, max_size=None, ping_interval=20,
                                               additional_headers={"Authorization": f"Bearer {self.api_key}"})
            await socket_.send(json.dumps(setup))
            refused = await asyncio.wait_for(self._await_setup(socket_), timeout=self.setup_timeout)
        except (asyncio.TimeoutError, websockets.WebSocketException, OSError) as exc:
            refused = f"{type(exc).__name__}: {exc}" if str(exc) else "no setup answer"
        if refused:
            if socket_ is not None:
                await socket_.close()
            raise RuntimeError(f"speech-to-text {name}: {refused[:160]}")
        return socket_

    async def open(self, on_event) -> None:
        if not self.api_key and not self.url:
            raise RuntimeError("OPENAI_API_KEY missing (speech-to-text)")
        self._event = on_event
        self._closing = False
        wanted = [self._connect(self.url or STT_URL, self._setup_message(), self.model)]
        if self.fast:
            wanted.append(self._connect(self.live_url or self.url or STT_URL, self._setup_message(live=True), STT_LIVE))
        opened = await asyncio.gather(*wanted, return_exceptions=True)
        if isinstance(opened[0], BaseException):
            for socket_ in opened[1:]:
                if not isinstance(socket_, BaseException):
                    await socket_.close()
            raise opened[0]
        self._socket = opened[0]
        self._tasks = [asyncio.create_task(self._receive())]
        if self.fast and isinstance(opened[1], BaseException):
            log.warning("%s; going on without the quick start", opened[1])       # the first session does all of it
        elif self.fast:
            self._live = opened[1]
            self._tasks.append(asyncio.create_task(self._receive_live()))

    @staticmethod
    async def _await_setup(socket_) -> str:
        async for raw in socket_:
            message = json.loads(raw)
            if message.get("type") == "session.updated":
                return ""
            if message.get("type") == "error":
                return (message.get("error") or {}).get("message") or json.dumps(message)
        return "server closed the session"

    async def send(self, pcm: bytes) -> None:
        if self._socket is None:
            raise ConnectionError("session closed")
        audio, self._last = upsample(pcm, self._last)
        message = json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(audio).decode("ascii")})
        try:
            await self._socket.send(message)
        except websockets.ConnectionClosed as exc:
            raise ConnectionError(f"session closed: {exc}") from exc
        if self._live is not None:
            # The live session is paid by the minute, so it only gets the sentences: the last
            # second is kept back and goes up when the first session says someone is talking.
            if self._talking:
                await self._to_live(message, len(audio) / 2 / API_RATE)
            else:
                self._recent.append((message, len(audio) / 2 / API_RATE))

    async def _to_live(self, message: str, seconds: float = 0.0) -> None:
        self.live_seconds += seconds
        try:
            await self._live.send(message)
        except (websockets.ConnectionClosed, AttributeError):
            self._live = None                        # gone: the first session's text is used from here on

    async def close(self) -> None:
        self._closing = True
        sockets, self._socket, self._live = (self._socket, self._live), None, None
        for socket_ in sockets:
            if socket_ is not None:
                await socket_.close()
        tasks, self._tasks = [task for task in self._tasks if task is not asyncio.current_task()], []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _receive(self) -> None:
        reason = "speech-to-text closed the session"
        try:
            async for raw in self._socket:
                message = json.loads(raw)
                kind = message.get("type", "")
                if kind == "input_audio_buffer.speech_started":
                    self._partial, self._talking = "", True
                    while self._recent and self._live is not None:
                        await self._to_live(*self._recent.popleft())
                    await self._event("speech_started", "")
                elif kind == "input_audio_buffer.speech_stopped":
                    self._talking = False
                    self._turn += 1
                    guess, self._partial = self._partial.strip(), ""
                    await self._event("speech_stopped", "")
                    if self._live is not None:
                        await self._to_live(json.dumps({"type": "input_audio_buffer.commit"}))
                        if guess:
                            await self._event("guess", guess)
                elif kind == "input_audio_buffer.committed":
                    self._items[message.get("item_id", "")] = self._turn
                elif kind == "conversation.item.input_audio_transcription.completed":
                    self._count(message.get("usage") or {})
                    await self._final(message)
                elif kind == "conversation.item.input_audio_transcription.failed":
                    await self._final(message)           # the turn ended, with nothing to answer
                elif kind == "error":
                    await self._event("error", (message.get("error") or {}).get("message") or json.dumps(message)[:200])
        except websockets.ConnectionClosed as exc:
            reason = f"speech-to-text connection lost: {exc}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:                     # a bad message must be visible, not silent
            log.exception("speech-to-text receive failed")
            reason = f"{type(exc).__name__}: {exc}"
        if not self._closing:
            self._socket = None
            await self._event("closed", reason)

    async def _receive_live(self) -> None:
        try:
            async for raw in self._live:
                message = json.loads(raw)
                kind = message.get("type", "")
                if kind == "conversation.item.input_audio_transcription.delta":
                    self._partial += message.get("delta") or ""
                elif kind == "input_audio_buffer.committed":
                    self._items[message.get("item_id", "")] = self._turn
                elif kind == "conversation.item.input_audio_transcription.completed":
                    await self._final(message)
                elif kind == "error":                # for one: a commit with no sound in it
                    log.debug("live speech-to-text: %s", (message.get("error") or {}).get("message"))
        except (websockets.ConnectionClosed, AttributeError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("live speech-to-text receive failed")
        self._live = None

    async def _final(self, message: dict) -> None:
        """The finished text of one sentence, from either session: the first with words wins."""
        turn = self._items.pop(message.get("item_id", ""), self._turn)
        text = (message.get("transcript") or "").strip()
        if turn <= self._done:
            return
        if not text and self._live is not None and self._empty != turn:
            self._empty = turn                       # the other session may still have heard words
            return
        self._done = turn
        await self._event("text", text)

    def _count(self, used: dict) -> None:
        if used.get("type") == "duration":           # whisper-style billing: seconds, no tokens
            audio, text, out = round(float(used.get("seconds") or 0) * 10), 0, 0
        else:
            into = used.get("input_token_details") or {}
            audio, text = int(into.get("audio_tokens") or 0), int(into.get("text_tokens") or 0)
            out = int(used.get("output_tokens") or 0)
        usage.add(self.usage, {"promptTokensDetails": [{"modality": "AUDIO", "tokenCount": audio},
                                                       {"modality": "TEXT", "tokenCount": text}],
                               "responseTokensDetails": [{"modality": "TEXT", "tokenCount": out}]})

    def live_meter(self) -> dict:
        """The live session's sound since the last call, as audio tokens (10 a second; the price
        family carries OpenAI's $0.017 a minute). Counted from what was sent to it."""
        seconds, self.live_seconds = self.live_seconds, 0.0
        return {"promptTokensDetails": [{"modality": "AUDIO", "tokenCount": round(seconds * 10)}]} if seconds else {}


# ---- Brain: a text model with tools over the OpenAI chat-completions wire format --------------
def chat_usage(used: dict) -> dict:
    """A chat completion's `usage` -> the usageMetadata shape willie/usage.py reads."""
    cached = int((used.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    prompt = max(0, int(used.get("prompt_tokens") or 0) - cached) + round(cached * CACHED_AS_TEXT)
    return {"promptTokensDetails": [{"modality": "TEXT", "tokenCount": prompt}],
            "responseTokensDetails": [{"modality": "TEXT", "tokenCount": int(used.get("completion_tokens") or 0)}]}


class Brain:
    """`stream(messages, tools)` yields ("text", piece), then at most one ("calls", [...]) and
    one ("usage", {...}). Every provider in PROVIDERS speaks this format, so DeepSeek, Groq,
    OpenRouter or a model on the home server is a setting, not code."""

    def __init__(self, spec: str, local_url: str = "", base_url: str | None = None, keys: list[str] | None = None):
        self.provider, self.model, self.effort = parse_spec(spec)
        if self.provider not in PROVIDERS or not self.model:
            raise ValueError(f"voice.llm: {spec!r} is not provider:model (providers: {', '.join(PROVIDERS)})")
        base = base_url or (local_url if self.provider == "local" else PROVIDERS[self.provider][0])
        if not base:
            raise ValueError(f"voice.llm: {spec!r} needs voice.local_llm_url")
        self.keys = _keys(self.provider) if keys is None else keys
        if not self.keys and self.provider != "local" and base_url is None:
            raise ValueError(f"voice.llm: no key for {self.provider} ({' / '.join(PROVIDERS[self.provider][1])} in .env)")
        self.http = Http(base)
        self._key = 0                # which of the keys works (the free Gemini key can be refused)

    @property
    def key_kind(self) -> str:
        return voice_keys.key_kind(self.keys[self._key]) if self.provider == "gemini" and self.keys else "betaald"

    def warm(self) -> None:
        self.http.warm()

    def _body(self, messages: list[dict], tools: list[dict], force: str = "") -> dict:
        body = {"model": self.model, "messages": messages, "stream": True, "stream_options": {"include_usage": True}}
        if tools:
            body["tools"] = [{"type": "function", "function": tool} for tool in tools]
        if force:                                    # the answer must be this tool call (the screen model)
            body["tool_choice"] = {"type": "function", "function": {"name": force}}
        if self.effort:
            body["reasoning_effort"] = self.effort
        return body

    def _request(self, messages: list[dict], tools: list[dict], force: str = "") -> http.client.HTTPResponse:
        while True:
            headers = {"Authorization": f"Bearer {self.keys[self._key]}"} if self.keys else {}
            response = self.http.post("/chat/completions", headers, self._body(messages, tools, force))
            if response.status == 200:
                return response
            text = _error_text(response)
            self.http.drop()
            if response.status in (401, 403, 429) and self._key + 1 < len(self.keys):
                log.warning("%s: key %d refused (%s), trying the next", self.provider, self._key + 1, text[:80])
                self._key += 1                   # the free key is over its limit: the paid one, for this session
                continue
            if response.status == 400 and self.effort and re.search("reasoning|thinking", text, re.I):
                log.warning("%s does not take reasoning_effort=%s: %s", self.model, self.effort, text[:80])
                self.effort = ""
                continue
            raise StageError(f"{self.model}: HTTP {response.status}: {text}", response.status)

    def _work(self, messages: list[dict], tools: list[dict], put, force: str = "") -> None:
        response = self._request(messages, tools, force)
        calls: list[dict] = []
        used = None
        for raw in response:
            line = raw.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                break
            chunk = json.loads(data)
            if chunk.get("error"):
                raise StageError(f"{self.model}: {str(chunk['error'])[:300]}")
            used = chunk.get("usage") or used
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    put(("text", delta["content"]))
                for call in delta.get("tool_calls") or []:
                    _merge_call(calls, call)
        response.read()                          # to the end: the connection is kept for the next turn
        if calls:
            for n, call in enumerate(calls):
                call["id"] = call["id"] or f"call_{n}"
            put(("calls", calls))
        if used:
            put(("usage", chat_usage(used)))

    def stream(self, messages: list[dict], tools: list[dict], force: str = ""):
        return in_thread(lambda put: self._work(messages, tools, put, force), self.http.drop)

    def close(self) -> None:
        self.http.drop()


def _merge_call(calls: list[dict], piece: dict) -> None:
    """One streamed tool-call fragment into `calls`. OpenAI sends a call in pieces under one
    index; Gemini sends each call whole, sometimes all under index 0, so a new id is a new call.
    Fields this code does not know (Gemini's thought signature in extra_content) are kept as
    they came: the model wants them back with the tool result."""
    index, call_id = piece.get("index"), piece.get("id") or ""
    slot = calls[index] if isinstance(index, int) and index < len(calls) else None
    if slot is None or (call_id and slot["id"] and call_id != slot["id"]):
        slot = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
        calls.append(slot)
    function = piece.get("function") or {}
    slot["id"] = slot["id"] or call_id
    slot["function"]["name"] = slot["function"]["name"] or function.get("name") or ""
    slot["function"]["arguments"] += function.get("arguments") or ""
    for key, value in piece.items():
        if key not in ("index", "id", "type", "function"):
            slot[key] = value


# ---- Mouth: text-to-speech, one request per piece of text, sound streamed back ----------------
class Mouth:
    """`stream(text)` yields 24 kHz mono S16. "openai:model" and "local:model" use the
    /audio/speech call (Kokoro-FastAPI and other home servers speak it too); "gemini" is his
    old Iapetus voice, a whole sentence at a time (1-3 s), kept as the fallback."""
    rate = API_RATE

    def __init__(self, spec: str, voice: str = "", style: str = "", speed: float = 1.0, local_url: str = "",
                 base_url: str | None = None, keys: list[str] | None = None):
        self.provider, self.model, _ = parse_spec(spec)
        self.voice, self.style, self.speed = voice or VOICE, style, float(speed or 1.0)
        self.seconds, self.chars = 0.0, 0
        self.http = None
        if self.provider == "gemini":
            self.keys = voice_keys.talk_keys() if keys is None else keys
            if not self.keys:
                raise ValueError("voice.tts: gemini needs GEMINI_API_KEY")
            return
        if self.provider not in ("openai", "local"):
            raise ValueError(f"voice.tts: {spec!r} is not openai:model, local:model or gemini")
        base = base_url or (local_url if self.provider == "local" else PROVIDERS["openai"][0])
        if not base or not self.model:
            raise ValueError(f"voice.tts: {spec!r} needs a model{' and voice.local_tts_url' if self.provider == 'local' else ''}")
        self.keys = _keys(self.provider) if keys is None else keys
        if not self.keys and self.provider == "openai" and base_url is None:
            raise ValueError("voice.tts: openai needs OPENAI_API_KEY")
        self.http = Http(base)

    def warm(self) -> None:
        if self.http:
            self.http.warm()

    def _body(self, text: str) -> dict:
        body = {"model": self.model, "voice": self.voice, "input": text, "response_format": "pcm"}
        if "gpt-" in self.model:
            # This model takes its manner as an instruction; its speed too (it ignores `speed`).
            pace = " Praat iets sneller dan normaal." if self.speed > 1.03 else " Praat rustig." if self.speed < 0.97 else ""
            if self.style or pace:
                body["instructions"] = (self.style + pace).strip()
        elif abs(self.speed - 1.0) > 0.01:
            body["speed"] = self.speed
        return body

    def _work(self, text: str, put) -> None:
        if self.http is None:                        # Gemini: the whole sentence comes back at once
            from willie.audio import speech
            pcm = speech.gemini_pcm(text, self.keys[0])
            for at in range(0, len(pcm), 4800):
                put(pcm[at:at + 4800])
            return
        headers = {"Authorization": f"Bearer {self.keys[0]}"} if self.keys else {}
        response = self.http.post("/audio/speech", headers, self._body(text))
        if response.status != 200:
            text_ = _error_text(response)
            self.http.drop()
            raise StageError(f"{self.model}: HTTP {response.status}: {text_}", response.status)
        odd = b""
        size = 2400                                  # 50 ms first, so the first sound leaves at once
        while True:
            chunk = response.read(size)
            if not chunk:
                break
            chunk, odd = odd + chunk, b""
            if len(chunk) % 2:
                chunk, odd = chunk[:-1], chunk[-1:]
            if chunk:
                put(chunk)
            size = 4800

    async def stream(self, text: str):
        self.chars += len(text)
        async for pcm in in_thread(lambda put: self._work(text, put), self.http.drop if self.http else lambda: None):
            self.seconds += len(pcm) / 2 / self.rate
            yield pcm

    def meter(self) -> dict:
        """What was spoken since the last call, in the meter's shape (an estimate, see TTS_TOKENS_PER_S)."""
        seconds, chars, self.seconds, self.chars = self.seconds, self.chars, 0.0, 0
        if not seconds:
            return {}
        return {"promptTokensDetails": [{"modality": "TEXT", "tokenCount": chars // 4}],
                "responseTokensDetails": [{"modality": "AUDIO", "tokenCount": round(seconds * TTS_TOKENS_PER_S)}]}

    def close(self) -> None:
        if self.http:
            self.http.drop()


# ---- the model's text -> pieces the speech engine can start on --------------------------------
_MARKUP = re.compile(r"[*_`#~]+|^\s*[-•]\s+|\[(.*?)\]\(.*?\)|https?://\S+|[\U0001F000-\U0001FAFF☀-➿️]", re.M)
_END = re.compile(r"[.!?…]+[\"')\]]*\s+|\n+")
# A full stop after one of these is not the end of a sentence ("3. ", "bijv. ", "o.a. ").
_NOT_END = re.compile(r"(?:^|[\s(])(?:\d+|[A-Za-z]|bijv|bv|o\.a|d\.w\.z|nr|ca|etc|evt|incl|excl|max|min|vs|e\.g|i\.e|approx)\.\s*$")
MIN_PIECE = 18               # shorter than this waits for the next sentence ("Ja." alone sounds chopped)
MIN_FIRST = 4                # but not the first one of an answer: "Dat kan." is sound half a second sooner
FIRST_LONG = 90              # the first sentence may be cut at a comma when it runs past this


def _letters(text: str) -> str:
    """Two writings of one sentence compare equal: no capitals, punctuation, spaces or hyphens."""
    return "".join(char for char in text.lower() if char.isalnum())


def clean(text: str) -> str:
    return " ".join(_MARKUP.sub(lambda found: found.group(1) or "", text).split())


class Split:
    """The model's text as it comes -> the part that is spoken. What follows the [SCHERM] line is
    kept in `notes` for the screen (None = there was no such line)."""

    def __init__(self):
        self.notes: str | None = None
        self._held = ""              # the end of the text so far, when it could be the start of the mark

    def feed(self, text: str) -> str:
        if self.notes is not None:
            self.notes += text
            return ""
        text, self._held = self._held + text, ""
        at = text.find(MARK)
        if at >= 0:
            self.notes = text[at + len(MARK):]
            return text[:at]
        for size in range(min(len(MARK) - 1, len(text)), 0, -1):
            if MARK.startswith(text[-size:]):
                self._held = text[-size:]
                return text[:-size]
        return text

    def flush(self) -> str:
        held, self._held = self._held, ""
        return held if self.notes is None else ""


class Sentences:
    def __init__(self, first: bool = True):
        self.buffer = ""
        self.first = first           # nothing spoken yet in this answer: the first piece is hurried

    def feed(self, text: str) -> list[str]:
        self.buffer += text
        out = []
        while True:
            cut = self._cut()
            if not cut:
                return out
            piece, self.buffer = clean(self.buffer[:cut]), self.buffer[cut:]
            if piece:
                out.append(piece)
                self.first = False

    def _cut(self) -> int:
        for found in _END.finditer(self.buffer):
            head = self.buffer[:found.end()]
            if found.group().strip() and _NOT_END.search(head):
                continue
            if len(head.strip()) >= (MIN_FIRST if self.first else MIN_PIECE):
                return found.end()
        if self.first and len(self.buffer) > FIRST_LONG:
            comma = self.buffer.rfind(", ", MIN_PIECE * 2, FIRST_LONG)
            if comma > 0:
                return comma + 2
        return 0

    def flush(self) -> list[str]:
        piece, self.buffer = clean(self.buffer), ""
        return [piece] if piece else []


# ---- the adapter ------------------------------------------------------------------------------
class CascadeAdapter(VoiceAdapter):
    name = "cascade"
    in_rate = 16_000
    out_rate = API_RATE

    def __init__(self, language: str = "nl", stt_model: str = "", llm: str = "", tts: str = "", voice: str = "",
                 style: str = "", speed: float = 1.0, hint: str = "", local_llm_url: str = "", local_tts_url: str = "",
                 noise_filter: bool = True, fast: bool = True, screen_llm: str = "", source: str = "robot",
                 search: bool = False, resume: bool = False, resume_handle: str = "", ears=None,
                 brains: list | None = None, mouths: list | None = None, screens: list | None = None):
        """`ears`, `brains` and `mouths` replace the real parts (the tests pass fakes). `search`,
        `resume` and `resume_handle` are taken so the runner builds every adapter the same way:
        he searches through zoek_op, and a garage conversation starts fresh after a reconnect."""
        super().__init__()
        self.language, self.source, self.noise_filter = language, source, noise_filter
        self.search, self.resume, self.resume_handle = False, False, ""
        self.ears = ears or Ears(stt_model, language, hint, fast=fast)
        self.brains = brains if brains is not None else self._build(chain_specs(llm or LLM), "voice.llm",
                                                                    lambda spec: Brain(spec, local_llm_url))
        style = style or STYLES.get(language, "")
        self.mouths = mouths if mouths is not None else self._build(
            chain_specs(tts or TTS), "voice.tts", lambda spec: Mouth(spec, voice, style, speed, local_tts_url))
        # The screen model is an extra: without a key for it he simply draws his cards himself again.
        self.screens = screens if screens is not None else []
        if screens is None and screen_llm:
            try:
                self.screens = self._build(chain_specs(screen_llm), "voice.screen_llm", lambda spec: Brain(spec, local_llm_url))
            except RuntimeError:
                pass
        self._split = False              # this session: notes after [SCHERM], drawn by the screen model
        self._drawing: asyncio.Task | None = None
        self.model = getattr(self.brains[0], "model", "cascade")
        self.key_kind = "betaald"        # the ears and the mouth are paid, whatever key the brain uses
        self.messages: list[dict] = []
        self.usage: dict[str, dict] = {}                 # brain model -> summed tokens
        self._kinds: dict[str, str] = {}                 # brain model -> gratis / betaald
        self._reply: asyncio.Task | None = None
        self._base = 0                   # where the running turn starts in `messages` (its user message)
        self._question = ""              # that user message, for when Wouter turns out not to be finished
        self._spoken: list[str] = []     # what of the running answer has reached the speaker
        self._speaking = False           # "speaking" sent for the running answer
        self._tools_ran = False
        self._opened = False             # the running turn has its question in `messages`
        self._closed = False
        self._clock: dict[str, float] = {}
        self._guess: str | None = None   # the running answer was started on a guess of the sentence: its letters
        self._confirmed: asyncio.Event | None = None     # ... and this says the finished text agreed
        self._quiet: asyncio.Event | None = None         # Wouter is not talking (as the ears hear it)

    @staticmethod
    def _build(specs: list[str], name: str, make) -> list:
        """The parts of a chain that can work here; one without its key is skipped with a line in the log."""
        parts, problems = [], []
        for spec in specs:
            try:
                parts.append(make(spec))
            except ValueError as exc:
                problems.append(str(exc))
                log.warning("%s", exc)
        if not parts:
            raise RuntimeError("; ".join(problems) or f"{name} is empty")
        return parts

    # ---- lifecycle -------------------------------------------------------------
    async def start_session(self, persona: str, context: str, tools: list[Tool]) -> None:
        self._register_tools(tools)
        # English-out is said before the Dutch persona and again after everything (as in the
        # OpenAI adapter: the Dutch examples pull him back to Dutch).
        first = "OUTPUT LANGUAGE: English only. The Dutch examples below show the manner, not the language." \
            if self.language == "nl_en" else ""
        runs_on = f"Je draait op het model {self.model} (je hoort met {getattr(self.ears, 'model', '?')})."
        self._split = bool(self.screens) and "kaart" in self.tools
        prompt = "\n\n".join(p for p in (first, persona, DISCIPLINE, TALK_LESS if self._split else SCREEN, SPOKEN, context, runs_on,
                                         LANGUAGES.get(self.language, "")) if p).strip()
        self.messages = [{"role": "system", "content": prompt}]
        self._quiet = asyncio.Event()
        self._quiet.set()
        # The handshakes of the brain and the mouth run while the ears connect.
        warming = [asyncio.to_thread(part.warm) for part in (self.brains[0], self.mouths[0], *self.screens[:1])
                   if hasattr(part, "warm")]
        opened = await asyncio.gather(self.ears.open(self._on_ears), *warming, return_exceptions=True)
        if isinstance(opened[0], BaseException):
            raise RuntimeError(str(opened[0])) from opened[0]
        voice_keys.SESSION.update(model=self.model, key=self.key_kind)
        if voice_keys.KEY_HOOK:
            try:
                voice_keys.KEY_HOOK(True)
            except Exception:
                pass
        self.is_open, self._closed = True, False
        await self._emit("ready", self.model)

    async def send_audio(self, pcm: bytes) -> None:
        if not self.is_open:
            raise ConnectionError("session closed")
        await self.ears.send(pcm)

    async def send_text(self, text: str) -> None:
        """A message from the robot itself (a timer, a button on a card): answered out loud,
        after what he is saying now."""
        if not self.is_open:
            raise ConnectionError("session closed")
        self._start(text, after=self._reply)

    async def interrupt(self) -> None:
        if await self._cancel():
            # The conversation keeps what he got to say, so "ga verder" knows where he was.
            self._forget(keep=1)
            if self._spoken:
                self.messages.append({"role": "assistant", "content": " ".join(self._spoken) + " [onderbroken]"})
            self._speaking = False
            await self._emit("interrupted")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._cancel()
        if self._drawing is not None and not self._drawing.done():
            self._drawing.cancel()
            await asyncio.gather(self._drawing, return_exceptions=True)
        self.is_open = False
        await self.ears.close()
        for part in (*self.brains, *self.mouths, *self.screens):
            if hasattr(part, "close"):
                part.close()
        self._finish()
        await self._emit("closed", "close()")

    def _finish(self) -> None:
        """Once per session: the three bills to the meter, the key badge off."""
        for model, counts in self.usage.items():
            usage.report(self.source, model, self._kinds.get(model, "betaald"), counts)
        self.usage = {}
        heard = getattr(self.ears, "usage", None)
        if heard:
            usage.report(self.source, self.ears.model, "betaald", heard)
            self.ears.usage = {}
        live = self.ears.live_meter() if hasattr(self.ears, "live_meter") else None
        if live:
            usage.report(self.source, STT_LIVE, "betaald", meta=live)
        self._meter_mouths()
        voice_keys.SESSION.update(model=None, key=None)
        if voice_keys.KEY_HOOK:
            try:
                voice_keys.KEY_HOOK(False)
            except Exception:
                pass

    def _meter_mouths(self) -> None:
        for mouth in self.mouths:
            # Gemini's speech reports itself (willie/audio/speech.py).
            if getattr(mouth, "provider", "") == "gemini" or not hasattr(mouth, "meter"):
                continue
            spoken = mouth.meter()
            if spoken:
                usage.report(self.source, mouth.model, "betaald", meta=spoken)

    # ---- the ears talk ---------------------------------------------------------
    async def _on_ears(self, kind: str, detail: str) -> None:
        if kind == "speech_started":
            self._quiet.clear()
            if self._speaking:                       # Wouter talks over him
                await self.interrupt()
        elif kind == "speech_stopped":
            self._quiet.set()
            self._clock = {"stop": time.monotonic()}
        elif kind == "guess":
            if self._sentence(detail) and (self._reply is None or self._reply.done()):
                self._clock["text"] = time.monotonic()
                self._guess = _letters(detail)
                self._start(detail, guess=True)
        elif kind == "text":
            await self._heard(detail)
        elif kind == "error":
            await self._emit("error", f"speech-to-text: {detail}")
        elif kind == "closed" and not self._closed:
            self._closed = True
            await self._cancel()
            self.is_open = False
            self._finish()
            await self._emit("closed", detail)

    def _sentence(self, text: str) -> bool:
        """Words, not a sound: "uh" and "hm" are not answered."""
        if not text or not any(char.isalnum() for char in text):
            return False
        if self.noise_filter:
            from willie.voice import local
            return not local.is_noise(local.normalise(text), 9.0)
        return True

    async def _heard(self, text: str) -> None:
        guess, self._guess = self._guess, None
        if guess is not None and self._reply is not None and not self._reply.done():
            if guess == _letters(text):              # the guess was right: the answer under way may be heard
                await self._emit("heard", text)
                self._confirmed.set()
                return
            await self._cancel()                     # it was not: nothing was said or done yet, start over
            self._forget()
        if not self._sentence(text):
            if text and any(char.isalnum() for char in text):
                await self._emit("dropped", text)
            return
        self._clock.setdefault("text", time.monotonic())
        await self._emit("heard", text)
        if self._reply is not None and not self._reply.done():
            if self._spoken or self._tools_ran:
                await self.interrupt()               # he was already answering: this is a new question
            else:
                # He was still thinking and Wouter went on: it was one sentence with a pause in
                # it. Start over with all of it instead of answering the first half.
                await self._cancel()
                self._forget()
                text = f"{self._question} {text}"
        self._start(text)

    def _start(self, text: str, guess: bool = False, after: asyncio.Task | None = None) -> None:
        self._confirmed = asyncio.Event()
        if not guess:
            self._confirmed.set()
        self._opened = False
        if after is None or after.done():
            self._open(text)                         # now, not in the task: a cancel must know what to take back
            after = None
        self._reply = asyncio.create_task(self._turn(text, after=after))

    def _open(self, text: str) -> None:
        self._trim()
        self._base, self._question, self._opened = len(self.messages), text, True
        self._spoken, self._tools_ran = [], False
        self.messages.append({"role": "user", "content": text})

    def _forget(self, keep: int = 0) -> None:
        """Take the cancelled turn out of the conversation again (`keep` 1 = leave its question in)."""
        if self._opened:
            del self.messages[self._base + keep:]
            self._opened = bool(keep)

    async def _go(self) -> None:
        """Before the first sound and before any tool: the sentence must be confirmed (see
        Ears.fast) and Wouter must have stopped talking - a pause is not the end of a sentence."""
        try:
            await asyncio.wait_for(self._confirmed.wait(), CONFIRM_S)
        except asyncio.TimeoutError:
            raise Unconfirmed() from None
        try:
            await asyncio.wait_for(self._quiet.wait(), QUIET_S)
        except asyncio.TimeoutError:                 # sound without end (a radio): answer anyway
            pass

    async def _cancel(self) -> bool:
        task = self._reply
        if task is None or task.done() or task is asyncio.current_task():
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    # ---- one question, one answer ----------------------------------------------
    async def _turn(self, text: str, after: asyncio.Task | None = None) -> None:
        if after is not None:                        # a message of his own waits for the answer he is giving
            await asyncio.gather(after, return_exceptions=True)
            self._open(text)
        try:
            await self._respond()
        except asyncio.CancelledError:
            raise
        except Unconfirmed:                          # the finished text never came: nothing was said or done
            self._guess = None
            self._forget()
        except Exception as exc:
            log.warning("no answer: %s", exc)
            self._forget(keep=1)
            if self._spoken:
                self.messages.append({"role": "assistant", "content": " ".join(self._spoken)})
            self._speaking = False
            await self._emit("error", f"{type(exc).__name__}: {exc}"[:300])

    async def _respond(self) -> None:
        pieces: asyncio.Queue = asyncio.Queue()
        voice = asyncio.create_task(self._speak(pieces))
        # With a screen model the brain does not get kaart: it writes notes, and the prompt is 3 kB smaller.
        declarations = [tool.declaration() for tool in self.tools.values() if not (self._split and tool.name == "kaart")]
        drawing = None
        try:
            for _ in range(MAX_ROUNDS):
                split, screen, text, calls = Sentences(first=not self._spoken), Split(), "", []
                async for kind, value in self._chain(self.brains, lambda brain: brain.stream(self.messages, declarations)):
                    if kind == "text":
                        self._clock.setdefault("llm", time.monotonic())
                        text += value
                        for piece in split.feed(screen.feed(value) if self._split else value):
                            pieces.put_nowait(piece)
                    elif kind == "calls":
                        calls = value
                    elif kind == "usage":
                        brain = self.brains[0]
                        usage.add(self.usage.setdefault(brain.model, {}), value)
                        self._kinds[brain.model] = getattr(brain, "key_kind", "betaald")
                for piece in [*split.feed(screen.flush()), *split.flush()]:
                    pieces.put_nowait(piece)
                if not calls and (screen.notes or "").strip():
                    drawing = self._drawing = asyncio.create_task(self._draw(self._question, screen.notes.strip()))
                message = {"role": "assistant", "content": text or None}
                if calls:
                    message["tool_calls"] = calls
                self.messages.append(message)
                if not calls or voice.done():
                    break
                await self._go()
                self._tools_ran = True
                # Shielded: a tool that is driving or saving finishes, also when he is interrupted.
                self.messages.extend(await asyncio.shield(asyncio.gather(*(self._tool(call) for call in calls))))
            pieces.put_nowait(None)
            await voice
        except BaseException:                        # cancelled or failed: no card for an answer that was not given
            if drawing is not None:
                drawing.cancel()
            raise
        finally:
            if not voice.done():
                voice.cancel()
                await asyncio.gather(voice, return_exceptions=True)
        self._speaking = False
        self._log_timing()
        await self._emit("turn_complete")

    async def _tool(self, call: dict) -> dict:
        function = call.get("function") or {}
        try:
            args = json.loads(function.get("arguments") or "{}")
        except ValueError:
            args = {}
        result = await self._run_tool(function.get("name", ""), args if isinstance(args, dict) else {})
        if self.language == "nl_en":                 # Dutch tool results pulled Dutch words into his English
            result = {**result, "language": ENGLISH_RESULT} if isinstance(result, dict) \
                else {"result": result, "language": ENGLISH_RESULT}
        content = json.dumps(result, ensure_ascii=False, default=str)
        return {"role": "tool", "tool_call_id": call.get("id"), "content": content[:TOOL_CHARS]}

    async def _draw(self, question: str, notes: str) -> None:
        """The notes of one answer -> one kaart on his face, by the screen model. Never raises:
        without a card he has still said the answer."""
        card = self.tools["kaart"]
        language = "English" if self.language in ("en", "nl_en") else "Dutch"
        messages = [{"role": "system", "content": f"{DRAW} All text on the card is {language}."},
                    {"role": "user", "content": f"Question: {question}\n\nNotes:\n{notes}"}]
        calls: list[dict] = []
        try:
            await self._go()
            async for kind, value in self._chain(self.screens, lambda brain: brain.stream(messages, [card.declaration()], force="kaart")):
                if kind == "calls":
                    calls = value
                elif kind == "usage":
                    brain = self.screens[0]
                    usage.add(self.usage.setdefault(brain.model, {}), value)
                    self._kinds[brain.model] = getattr(brain, "key_kind", "betaald")
            if calls:
                args = json.loads((calls[0].get("function") or {}).get("arguments") or "{}")
                await self._run_tool("kaart", args if isinstance(args, dict) else {})
            else:
                log.warning("no card: the screen model gave no kaart call")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("no card: %s", exc)

    async def _speak(self, pieces: asyncio.Queue) -> None:
        last = False
        while not last:
            piece = await pieces.get()
            if piece is None:
                return
            if self._spoken:
                # After the first piece: everything that is already written goes in one request
                # (one breath, fewer calls). The first piece went alone, to be heard soonest.
                while not pieces.empty() and len(piece) < 400:
                    more = pieces.get_nowait()
                    if more is None:
                        last = True
                        break
                    piece += " " + more
            started = False
            async for pcm in self._chain(self.mouths, lambda mouth: mouth.stream(piece)):
                if not started:
                    started = True
                    if not self._spoken:
                        await self._go()
                    self._clock.setdefault("sound", time.monotonic())
                    if not self._speaking:
                        self._speaking = True
                        await self._emit("speaking")
                    self._spoken.append(piece)
                    await self._emit("said", piece + " ")
                await self._deliver_audio(pcm)

    async def _chain(self, parts: list, start):
        """Items from the first part that works. One that fails before it gave anything is put
        last for the rest of the session and the next one takes over."""
        for attempt in range(len(parts)):
            part, given = parts[0], False
            try:
                async for item in start(part):
                    given = True
                    yield item
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if given or attempt == len(parts) - 1:
                    raise
                log.warning("%s failed (%s), the next one takes over", getattr(part, "model", part), exc)
                parts.append(parts.pop(0))

    def _trim(self) -> None:
        """Keep the system prompt and the newest questions. Cut in one large step, not one turn
        at a time: the provider caches the prompt by its start, and every cut moves that."""
        users = [n for n, message in enumerate(self.messages) if message["role"] == "user"]
        if len(users) > MAX_TURNS:
            del self.messages[1:users[len(users) - (MAX_TURNS - 8)]]

    def _log_timing(self) -> None:
        clock = self._clock
        if "stop" in clock and "sound" in clock:
            def ms(name: str, since: str) -> str:
                return f"{(clock[name] - clock[since]) * 1000:.0f}" if name in clock and since in clock else "?"
            log.info("reply: first sound %s ms after the end of speech (text %s, model +%s, speech +%s) on %s",
                     ms("sound", "stop"), ms("text", "stop"), ms("llm", "text"), ms("sound", "llm"),
                     getattr(self.brains[0], "model", "?"))
        self._clock = {}


def configured(source: str = "robot", **overrides) -> CascadeAdapter:
    """The adapter as willie.yaml describes it (voice.stt_model, voice.llm, voice.tts, ...)."""
    from willie.voice.gemini_live import configured_language, setting
    options = dict(
        language=configured_language(), stt_model=str(setting("voice.stt_model", "") or ""),
        hint=str(setting("voice.stt_hint", "") or ""), llm=str(setting("voice.llm", "") or ""),
        tts=str(setting("voice.tts", "") or ""), voice=str(setting("voice.openai_voice", "") or ""),
        style=str(setting("voice.tts_style", "") or ""), speed=setting("voice.speed", 1.0),
        fast=bool(setting("voice.stt_fast", True)), noise_filter=bool(setting("voice.noise_filter", True)),
        screen_llm=str(setting("voice.screen_llm", SCREEN_LLM) or ""),
        local_llm_url=str(setting("voice.local_llm_url", "") or ""),
        local_tts_url=str(setting("voice.local_tts_url", "") or ""), source=source)
    return CascadeAdapter(**{**options, **overrides})
