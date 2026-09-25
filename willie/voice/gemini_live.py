"""Adapter A (D4): Gemini Live behind the D3 `VoiceAdapter` interface.

Two layers in this file:

- `GeminiLiveAdapter` is the protocol only: one bidi WebSocket, audio up, audio down, tool
  calls, interruptions. It never touches a sound card, so it runs the same on the Pi, on the
  Mac and against the mock server in tests/test_voice_adapter.py.
- `session()` is the Pi runner the bench tools use (`make live-talk`, `make voice-pi`):
  arecord -> echo gate -> adapter -> aplay, plus the idle timeout.

Audio contract:
  up    16 kHz 16-bit mono PCM, captured by ALSA (its resampler is clean; the
        hand-rolled decimation tried on 20 Sep smeared consonants badly enough
        that the model could not understand Dutch at all)
  down  24 kHz 16-bit mono PCM, upsampled to 48 kHz before aplay, because this
        card plays 48 kHz and is silent at lower rates (B6/B7 bench finding)
"""

from __future__ import annotations

import willie.voice as voice_keys
from willie.voice import talk_key, talk_keys
import array
import asyncio
import base64
import json
import logging
import os
import subprocess
import threading
from datetime import datetime

import websockets

from willie import usage
from willie.audio import speech
from willie.voice import tools as willie_tools
from willie.voice.base import Tool, VoiceAdapter
from willie.voice.persona import VOICE, system_prompt

log = logging.getLogger("willie.voice.gemini")

HOST = "generativelanguage.googleapis.com"
PATH = "/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"

# Listed on the key 20 Sep, newest first, each with the uplink shape it wants.
# Probed on the bench that day: 3.8-live ignores audio sent as "mediaChunks"
# (the 2.5 shape) without any error - it just never answers - while 2.5 accepts
# either. So the shape travels with the model name rather than being guessed.
MODELS = (
    ("models/gemini-3.8-live", "audio"),
    ("models/gemini-3.1-flash-live-preview", "audio"),
    ("models/gemini-2.5-flash-native-audio-latest", "mediaChunks"),
)

IN_RATE, OUT_RATE, CARD_RATE = 16_000, 24_000, 48_000
CHUNK_MS = 100
DEVICE = "plughw:CARD=sndrpigooglevoi,DEV=0"

# Live-specific additions on top of config/persona.md: spoken turns are shorter
# than written ones, and the model needs telling not to slow down for clarity.
LIVE_EXTRA = (
    "Dit is een gesprek via een luidspreker. Praat in een vlot, stevig tempo, "
    "niet langzaam of overdreven duidelijk. Hou elke beurt kort: een of twee "
    "zinnen, daarna stil zijn. Val niet terug op beleefdheidsformules."
)


# voice.language (willie.yaml) -> BCP-47 code for speechConfig, plus a prompt line: the
# language code alone does not stop the model switching to English on an English word.
LANGUAGES = {
    "nl": ("nl-NL", "Spreek altijd Nederlands. Alleen als Wouter zelf Engels praat, antwoord je in het Engels."),
    "en": ("en-US", "Always speak English."),
}
DAYS = ("maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag")
MONTHS = ("januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus",
          "september", "oktober", "november", "december")


def _get(message: dict, camel: str, snake: str):
    """The Live API has answered in both spellings on different model versions."""
    return message.get(camel) or message.get(snake)


class GeminiLiveAdapter(VoiceAdapter):
    name = "gemini_live"
    in_rate = IN_RATE
    out_rate = OUT_RATE

    def __init__(self, api_key: str | None = None, model: str = "",
                 url: str | None = None, setup_timeout: float = 15.0, language: str = "nl",
                 search: bool = False, resume: bool = False, resume_handle: str = "",
                 source: str = "robot"):
        """`model` = one entry of MODELS (or any Live model; empty = try MODELS in order).
        `url` replaces the Google endpoint - the tests point it at a local mock server.
        `resume` (garage mode, K1): ask for session resumption + a sliding context window,
        so a conversation outlives Google's connection limit; `resume_handle` continues an
        earlier one. The newest handle is kept in `self.resume_handle`.
        `source` names the session in the token meter (willie/usage.py): robot, garage, telefoon."""
        super().__init__()
        self.api_key = api_key or talk_key()
        # The talk key (free) falls back to the paid key when every model refuses it.
        self.keys = talk_keys() if self.api_key == talk_key() else [self.api_key]
        self.models = [(m, s) for m, s in MODELS if m == model] or ([(model, "audio")] if model else list(MODELS))
        self.voice = VOICE
        self.language = language
        self.search = search            # Google Search grounding (built into the Live API)
        self.url = url
        self.setup_timeout = setup_timeout
        self.model = ""
        self._shape = "audio"
        self._socket = None
        self._receiver: asyncio.Task | None = None
        self._speaking = False           # "speaking" already sent for the current model turn
        self._dropping = False           # interrupt() was called: discard the rest of this turn
        self._closing = False
        self.resume = resume
        self.resume_handle = resume_handle
        self.source = source
        self.window = configured_window()   # (trigger, target) tokens, or None = no cap (L4)
        self.usage: dict = {}            # summed usageMetadata of this session (token meter)
        self.key_kind = ""

    # ---- lifecycle -------------------------------------------------------------
    async def start_session(self, persona: str, context: str, tools: list[Tool]) -> None:
        if not self.api_key and not self.url:
            raise RuntimeError("GEMINI_API_KEY missing")
        self._register_tools(tools)
        rule = LANGUAGES.get(self.language, ("", ""))[1]
        prompt = "\n\n".join(p for p in (persona, rule, context) if p).strip()
        last_error = "no model accepted the session"
        # Per model: free key, then paid - so he stays on the best model (3.8) either way.
        tries = [(key, model, shape) for model, shape in self.models for key in (self.keys or [self.api_key])]
        for key, model, shape in tries:
            if key != tries[0][0] and not self.url and key != getattr(self, "_fallback_logged", None):
                log.warning("free key refused (%s) - talking on the paid key", last_error[:120])
                self._fallback_logged = key
            self._key_now = key
            url = self.url or f"wss://{HOST}{PATH}?key={key}"
            try:
                socket = await websockets.connect(url, max_size=None, ping_interval=20)
            except (websockets.WebSocketException, OSError) as exc:
                last_error = f"{model}: {exc}"
                continue
            try:
                await socket.send(json.dumps(self._setup_message(model, prompt)))
                ack = json.loads(await asyncio.wait_for(socket.recv(), timeout=self.setup_timeout))
            except (asyncio.TimeoutError, websockets.WebSocketException, OSError) as exc:
                last_error = f"{model}: {exc or 'no setup answer'}"
                await socket.close()
                continue
            if "setupComplete" not in ack and "setup_complete" not in ack:
                last_error = f"{model}: {json.dumps(ack)[:160]}"
                await socket.close()
                continue
            self._socket, self.model, self._shape = socket, model, shape
            self.key_kind = voice_keys.key_kind(key)
            voice_keys.SESSION.update(model=model.removeprefix("models/"), key=self.key_kind)
            if voice_keys.KEY_HOOK:
                try:
                    voice_keys.KEY_HOOK(self.key_kind == "betaald")
                except Exception:
                    pass
            self.is_open, self._closing = True, False
            self._receiver = asyncio.create_task(self._receive())
            await self._emit("ready", model)
            return
        raise RuntimeError(last_error)

    def _setup_message(self, model: str, prompt: str) -> dict:
        setup = {
            "model": model,
            # Start of speech: HIGH since 25 Sep (Wouter: the mic has to be more
            # sensitive). LOW was there to ignore his own echo, but the runner's gate
            # keeps the uplink shut while he talks, and the mic is echo-cancelled now.
            "realtimeInputConfig": {
                "automaticActivityDetection": {
                    "startOfSpeechSensitivity": "START_SENSITIVITY_HIGH",
                    "endOfSpeechSensitivity": "END_SENSITIVITY_LOW",
                    "prefixPaddingMs": 300,
                    # 800 -> 600 ms (23 Sep, Wouter): answers sooner; lower risks cutting
                    # him off mid-thought.
                    "silenceDurationMs": 600,
                }
            },
            # Text of both sides: the face only shows "thinking" once real words were heard
            # (not a cough or a click), the follow-up window runs on words rather than noise,
            # and the conversation goes into long-term memory (willie/brain/memory.py).
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.voice}}},
            },
            # A model does not know its own name or version (it answered "1.5"), so say it.
            "systemInstruction": {"parts": [{"text": f"{prompt}\n\nJe draait op het model {model.split('/')[-1]}."}]},
        }
        code = LANGUAGES.get(self.language, ("", ""))[0]
        if code:
            setup["generationConfig"]["speechConfig"]["languageCode"] = code
        tools = []
        # Google's own search in the Live model only on the paid key: the free tier refuses a
        # 3.x session that asks for it ("quota exceeded", 25 Sep). The free key searches
        # through zoek_op (a 2.5 side session, free) instead; the paid key does not need that.
        native = self.search and voice_keys.key_kind(getattr(self, "_key_now", "") or self.api_key) == "betaald"
        if self.tools:
            tools.append({"functionDeclarations": [t.declaration() for t in self.tools.values()
                                                   if not (native and t.name == WEB_SEARCH.name)]})
        if native:
            # Free on the free tier; paid: 5,000 searches/month free, then $14 per 1,000 (21 Sep).
            tools.append({"googleSearch": {}})
        if tools:
            setup["tools"] = tools
        if self.resume:
            setup["sessionResumption"] = {"handle": self.resume_handle} if self.resume_handle else {}
            setup["contextWindowCompression"] = {"slidingWindow": {}}
        if self.window:
            # L4: every turn pays for the whole context, so cap it. Past `trigger` tokens the
            # server drops the oldest turns down to `target`; the system prompt is kept.
            trigger, target = self.window
            setup["contextWindowCompression"] = {"triggerTokens": trigger, "slidingWindow": {"targetTokens": target}}
        return {"setup": setup}

    async def send_audio(self, pcm: bytes) -> None:
        if not self.is_open or self._socket is None:
            raise ConnectionError("session closed")
        blob = {"mimeType": f"audio/pcm;rate={self.in_rate}", "data": base64.b64encode(pcm).decode("ascii")}
        inner = {"audio": blob} if self._shape == "audio" else {"mediaChunks": [blob]}
        try:
            await self._socket.send(json.dumps({"realtimeInput": inner}))
        except websockets.ConnectionClosed as exc:
            raise ConnectionError(f"session closed: {exc}") from exc

    async def end_audio(self) -> None:
        """The mic stream pauses (garage mode's gate closed): let the server's detector end
        the turn now instead of waiting for more audio."""
        if self.is_open and self._socket is not None:
            try:
                await self._socket.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))
            except websockets.ConnectionClosed:
                pass

    async def send_text(self, text: str) -> None:
        """A message from the robot itself (a timer, a danger he saw) as a user turn: the
        model answers it out loud in the same conversation."""
        if not self.is_open or self._socket is None:
            raise ConnectionError("session closed")
        turn = {"turns": [{"role": "user", "parts": [{"text": text}]}], "turnComplete": True}
        try:
            await self._socket.send(json.dumps({"clientContent": turn}))
        except websockets.ConnectionClosed as exc:
            raise ConnectionError(f"session closed: {exc}") from exc

    async def interrupt(self) -> None:
        """The Live API has no client-side "cancel": the server keeps generating the turn it
        started. So the rest of that turn is dropped here, until its turnComplete (or the
        server's own interruption) arrives."""
        if self._speaking:
            self._speaking = False
            self._dropping = True
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
            log.exception("gemini receive failed")
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
        meta = _get(message, "usageMetadata", "usage_metadata")
        if meta:
            usage.add(self.usage, meta)
        call = _get(message, "toolCall", "tool_call")
        if call:
            # Each call runs as its own task: the camera takes seconds and the
            # audio must keep flowing (and barge-in keep working) meanwhile.
            for function in _get(call, "functionCalls", "function_calls") or []:
                asyncio.create_task(self._answer_tool(function))
            return
        update = _get(message, "sessionResumptionUpdate", "session_resumption_update")
        if update:
            handle = _get(update, "newHandle", "new_handle")
            if handle and update.get("resumable", True):
                self.resume_handle = handle
            return
        if _get(message, "goAway", "go_away"):
            # With resumption this is routine: the runner reconnects with the handle.
            await self._emit("go_away" if self.resume else "error", "server will end the session soon (goAway)")
            return
        server = _get(message, "serverContent", "server_content") or {}
        heard = (_get(server, "inputTranscription", "input_transcription") or {}).get("text")
        if heard:
            await self._emit("heard", heard)
        said = (_get(server, "outputTranscription", "output_transcription") or {}).get("text")
        if said and not self._dropping:
            await self._emit("said", said)
        if server.get("interrupted"):                # the user talked over him (server VAD)
            if self._speaking:
                await self._emit("interrupted")
            self._speaking = self._dropping = False
            return
        turn = _get(server, "modelTurn", "model_turn") or {}
        for part in turn.get("parts", []):
            blob = _get(part, "inlineData", "inline_data")
            if blob and blob.get("data") and not self._dropping:
                if not self._speaking:
                    self._speaking = True
                    await self._emit("speaking")
                await self._deliver_audio(base64.b64decode(blob["data"]))
        if _get(server, "turnComplete", "turn_complete"):
            if self._dropping:
                self._dropping = False               # end of the turn interrupt() cut off
            else:
                await self._emit("turn_complete")
            self._speaking = False

    async def _answer_tool(self, function: dict) -> None:
        name = function.get("name", "")
        result = await self._run_tool(name, function.get("args") or {})
        if not isinstance(result, dict):
            result = {"result": result}
        response = {"functionResponses": [{"id": function.get("id"), "name": name, "response": result}]}
        if self._socket is not None:
            try:
                await self._socket.send(json.dumps({"toolResponse": response}))
            except websockets.ConnectionClosed:
                pass


# ======================================================================= Pi runner
# Everything below is sound-card plumbing for the bench tools; the adapter above
# does not depend on it.

def _upsample(pcm: bytes, rate: int = OUT_RATE) -> bytes:
    """Repeat samples up to the card's 48 kHz. Integer factors only."""
    factor = max(1, CARD_RATE // rate)
    if factor == 1:
        return pcm
    import numpy as np
    return np.repeat(np.frombuffer(pcm[: len(pcm) // 2 * 2], "<i2"), factor).tobytes()


class Speaker:
    """One long-lived aplay process, so words do not get a gap between chunks."""

    def __init__(self, echo=None) -> None:
        self.process: subprocess.Popen | None = None
        # When the audio written so far will have finished playing. aplay buffers,
        # so this is tracked from the byte count rather than from the last write.
        self.busy_until = 0.0
        self.echo = echo                 # willie.audio.clean.EchoReference, when AEC is on

    def speaking(self, now: float) -> bool:
        # busy_until is when aplay was handed the last sample; it sounds `delay` later
        # (0.3-0.5 s, tracked by the echo canceller). Without that the gate reopened while
        # his last words still came out, and he answered his own echo again (25 Sep).
        lag = self.echo.delay if self.echo is not None else ECHO_LAG_S
        return now < self.busy_until + lag + BARGE_IN_TAIL

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return
        self.process = subprocess.Popen(
            ["aplay", "-q", "-D", DEVICE, "-f", "S16_LE", "-r", str(CARD_RATE), "-c", "1", "-t", "raw"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def write(self, pcm: bytes) -> None:
        self.start()
        try:
            if self.process and self.process.stdin:
                scaled = speech.scale(pcm)
                data = _upsample(scaled)
                seconds = len(data) / (CARD_RATE * 2)
                loop = asyncio.get_event_loop()
                starts = max(self.busy_until, loop.time())
                if self.echo is not None:
                    self.echo.play(scaled, OUT_RATE, starts)
                self.busy_until = starts + seconds
                self.process.stdin.write(data)
                self.process.stdin.flush()
        except (BrokenPipeError, ValueError):
            self.process = None

    def stop(self) -> None:
        """Cut playback dead - used when the model is interrupted (barge-in)."""
        if self.process and self.process.poll() is None:
            self.process.kill()
        self.process = None
        self.busy_until = 0.0
        if self.echo is not None:
            self.echo.cut(asyncio.get_event_loop().time())


# The live mic uses the same capture as the wake word (willie/audio/mic.py): left channel
# only, x voice.wake_gain. Before 21 Sep it captured mono, which halved the INMP441's
# already quiet signal: Wouter's voice read 2-3 % FS, below the old fixed 5 % speech
# threshold, so the face never saw him talk (no listening -> thinking switch).
#
# Speech vs room is now relative: the room level is the 20th percentile of the last ~5 s
# of chunk peaks, and a chunk counts as speech when it is well above that.
SPEECH_OVER_ROOM = 2.5
SPEECH_MIN = 0.10            # never call anything under 10 % FS (after gain) speech
TURN_END_S = 0.8             # this much quiet after speech = his turn is over -> thinking
SPEECH_MIN_S = 0.4           # shorter bursts (a cough, a click, a cup on the desk) are no turn
PREROLL_MAX_S = 15.0         # audio kept while the session opens (the one-breath question)

# The microphone hears the speaker: they sit on the same board. While he is talking the
# uplink is gated, or the server's voice detector hears WILL-E himself and cuts him off.
# Without echo cancellation his echo clips at the wake gain, so the gate lets nothing
# through (2.0 > full scale; D4). With it (voice.aec, 25 Sep) clean.DoubleTalk decides:
# voice.barge_in = how many times louder than his expected leftover echo a voice must be.
# WILLIE_BARGE_IN_LEVEL (0-1 FS) is the no-AEC gate level, to experiment.
BARGE_IN_FS = float(os.environ.get("WILLIE_BARGE_IN_LEVEL", "2.0"))
# Talking over him takes this many loud chunks in a row (300 ms); they are then sent
# together. Single blips of leftover echo, e.g. when his audio arrives choppy and the
# echo delay jumps, never reach the server this way (bench, 25 Sep).
BARGE_IN_CHUNKS = 3
# Keep the gate shut a moment after the audio ends, for the tail out of the cone.
BARGE_IN_TAIL = 0.4
# Playback latency when the echo canceller is off (bench 25 Sep: 0.29-0.47 s).
ECHO_LAG_S = 0.5
# A tool's wrap-up (WRAP_UP) ends the session at most this long after it was asked.
WRAP_UP_MAX_S = 12.0


def _peak(chunk: bytes) -> float:
    samples = array.array("h")
    samples.frombytes(chunk[: len(chunk) // 2 * 2])
    return max(max(samples), -min(samples)) / 32768 if samples else 0.0


class SpeechDetector:
    """Is this chunk the user talking? Relative to the room, which it keeps measuring."""

    def __init__(self, window: int = 50):
        from collections import deque
        self.recent = deque(maxlen=window)

    def room(self) -> float:
        if len(self.recent) < 5:
            return SPEECH_MIN / SPEECH_OVER_ROOM
        return sorted(self.recent)[len(self.recent) // 5]

    def is_speech(self, peak: float) -> bool:
        speech = peak >= max(SPEECH_MIN, self.room() * SPEECH_OVER_ROOM)
        self.recent.append(peak)
        return speech


class Standby:
    """Not-for-me mode (23 Sep, Wouter): the session stays connected but nothing reaches the
    model; the wake word runs locally on the same mic chunks, and "Hey Willie" resumes the
    conversation at once - no reconnect, and the model still has the context."""

    def __init__(self) -> None:
        from collections import deque
        self.on = False
        self.since = 0.0
        self.spotter = None
        self.recent: deque[bytes] = deque(maxlen=15)     # 1.5 s: "Hey Willie" itself goes up too


async def _microphone(adapter: VoiceAdapter, speaker: Speaker, stop: asyncio.Event,
                      activity: list[float], on_event, ready: asyncio.Event | None = None,
                      recorder: subprocess.Popen | None = None, standby: Standby | None = None,
                      gate=None) -> None:
    """Mic -> adapter. Starts before the session is open: what Wouter says straight after
    "Hey Willie" is kept (`ready` not set yet) and sent the moment the session is ready, so
    "Hey Willie, hoe laat is het?" works in one breath (23 Sep). `recorder` is the wake
    word's own arecord, handed over still running, so not a word falls in the gap.
    `gate` (garage mode, K1: willie/voice/gate.py) decides per utterance what goes up."""
    from collections import deque
    from willie.audio import clean, mic
    frames = IN_RATE * CHUNK_MS // 1000
    factor = mic.gain()
    stream = mic.Stream(factor)
    settings = configured_clean()
    cleaner = clean.Cleaner.shared(settings["denoise_db"], settings["aec"]) if settings["clean"] else None
    if cleaner is None or cleaner.echo is None:
        speaker.echo = None
        barge_in = BARGE_IN_FS
    else:
        speaker.echo = clean.EchoReference(settings["aec_delay_ms"] / 1000)
        aligner = clean.Aligner(speaker.echo)
        barge_in = settings["barge_in"]
    # Capture clock for the echo reference: sample n of this arecord was taken at
    # t0 + n / IN_RATE. A read can only return a sample after it was taken, so the
    # smallest "now - samples so far" is the best estimate (a backlog only raises it).
    captured, t0 = 0, float("inf")
    loud: list[bytes] = []                  # barge-in candidates while he talks
    detector = SpeechDetector()
    sent, loudest = 0, 0.0
    user_speaking, speech_start, last_speech, turn_open = False, 0.0, 0.0, False
    preroll: deque[bytes] = deque(maxlen=int(PREROLL_MAX_S * 1000 / CHUNK_MS))
    loop = asyncio.get_running_loop()
    if recorder is not None:
        async def read(size: int) -> bytes:
            data = await asyncio.to_thread(recorder.stdout.read, size)
            if len(data) < size:
                raise asyncio.IncompleteReadError(data, size)
            return data
    else:
        process = await asyncio.create_subprocess_exec(
            *mic.command(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        read = process.stdout.readexactly
    on_event("mic_active", "")
    try:
        if recorder is None:
            # The first ~0.8 s after arecord opens is the mic settling (a loud decaying bump).
            await read(int(IN_RATE * 0.8) * mic.FRAME)
        while not stop.is_set():
            try:
                raw = await read(frames * mic.FRAME)
            except asyncio.IncompleteReadError:
                break
            decimated = stream.decimate(raw)
            chunk = mic.s16(decimated, factor)    # fixed gain: level detector, wake word, garage gate
            captured += len(decimated)
            t0 = min(t0, loop.time() - captured / IN_RATE)
            up = chunk
            if cleaner is not None:
                played = None
                if speaker.echo is not None:
                    played = speaker.echo.read(t0 + (captured - len(decimated)) / IN_RATE, len(decimated))
                    aligner.feed(decimated, played)
                up = cleaner.process(mic.s16(decimated, clean.IN_GAIN), played)
            peak = _peak(chunk)
            if ready is not None and not ready.is_set():
                preroll.append(up)
                continue
            while preroll:
                await adapter.send_audio(preroll.popleft())
            if standby is not None:
                standby.recent.append(up)
                if standby.on:
                    if standby.spotter is None:
                        from willie.voice import wake
                        standby.spotter = wake.Spotter()
                    if standby.spotter.feed(chunk):
                        standby.on = False
                        on_event("wake_again", "")
                        for held in standby.recent:
                            await adapter.send_audio(held)
                    continue
            # While he is speaking, swallow anything that is not clearly louder
            # than his own voice coming back through the microphone (after AEC: than
            # what is left of it).
            if speaker.speaking(loop.time()):
                if speaker.echo is not None:
                    # 50 = off: at volume 0.8 his leftover echo is as loud as Wouter at 1 m
                    # (bench with his real voice, 25 Sep), so he interrupted himself.
                    over = barge_in < 50 and cleaner.doubletalk.feed(cleaner.residual, played, barge_in)
                else:
                    over = peak >= barge_in
                if not over:
                    loud.clear()
                    continue
                loud.append(up)
                if len(loud) < BARGE_IN_CHUNKS:
                    continue
                if len(loud) == BARGE_IN_CHUNKS:    # through: the held start of the words too
                    for held in loud[:-1]:
                        await adapter.send_audio(held)
            now = loop.time()
            speech = detector.is_speech(peak)
            if gate is None:
                await adapter.send_audio(up)
            else:
                from willie.voice.gate import END
                for out in gate.feed(chunk, speech, now):
                    if out == END:
                        await adapter.end_audio()
                    else:
                        await adapter.send_audio(out)
            sent += 1
            loudest = max(loudest, peak)
            if speech:
                last_speech = now
                if not user_speaking:
                    user_speaking, speech_start = True, now
                # Garage mode: only a voice the gate let through makes the face listen.
                if (not turn_open and now - speech_start >= SPEECH_MIN_S
                        and (gate is None or gate.state == "open")):
                    turn_open = True
                    on_event("user_speaking", "")
            elif user_speaking and now - last_speech >= TURN_END_S:
                user_speaking = False
                if turn_open:
                    turn_open = False
                    on_event("user_turn_end", "")
            if sent % 20 == 0:                       # every 2 s
                on_event("uplink", f"{sent} chunks, loudest {loudest * 100:.1f}% FS, "
                                   f"room {detector.room() * 100:.1f}%")
                loudest = 0.0
    finally:
        on_event("mic_idle", "")
        speaker.echo = None
        if recorder is not None:
            recorder.terminate()
            await asyncio.to_thread(recorder.wait)
        elif process.returncode is None:
            process.terminate()
            await process.wait()


def show(args: dict, face=None) -> dict:
    """Update the session's face owner; never open a competing framebuffer writer."""
    text = str(args.get("tekst", "")).strip()
    if not text:
        return {"fout": "geen tekst"}
    if face is not None:
        return face.show(text)
    return {"fout": "geen scherm beschikbaar"}


SHOW = Tool(
    "toon",
    "Zet een korte tekst op je gezicht (het scherm): een getal, een maat, een pinout-regel, "
    "een lijstje. Gebruik dit als Wouter vraagt iets te laten zien of op te schrijven.",
    {"type": "object", "properties": {"tekst": {"type": "string", "description": "Maximaal ~180 tekens."}},
     "required": ["tekst"]},
    handler=lambda args: asyncio.to_thread(show, args),
)


def show_picture(args: dict, face=None) -> dict:
    """Find a photo of the subject on Wikipedia and put it on the face."""
    subject = str(args.get("onderwerp", "")).strip()
    if not subject:
        return {"fout": "geen onderwerp"}
    if face is None:
        return {"fout": "geen scherm beschikbaar"}
    from willie.face import picture
    try:
        found = picture.find(subject, str(args.get("onderwerp_en", "")))
    except (RuntimeError, OSError, ValueError) as exc:
        return {"fout": f"geen foto gevonden: {exc}"}
    face.show_image(found)
    return {"getoond": found.title, "bron": found.source}


SHOW_PICTURE = Tool(
    "toon_afbeelding",
    "Zoek een foto van iets op Wikipedia en zet die op je scherm. Gebruik dit als Wouter "
    "vraagt hoe iets eruitziet of iets wil zien: een dier, een ding, een plek, een onderdeel. "
    "Niet voor dingen die voor je staan: daarvoor is kijk.",
    {"type": "object", "properties": {
        "onderwerp": {"type": "string",
                      "description": "Kort onderwerp zoals een Nederlandse Wikipedia-titel, bijvoorbeeld 'banaan' of 'ESP32'."},
        "onderwerp_en": {"type": "string",
                         "description": "Hetzelfde onderwerp in het Engels, bijvoorbeeld 'banana' of 'stepper motor'."}},
     "required": ["onderwerp", "onderwerp_en"]},
)


WEB_SEARCH = Tool(
    "zoek_op",
    "Zoek actuele informatie op internet (Google): nieuws, uitslagen, prijzen, versies, "
    "tijden, weer, alles wat na je training veranderd kan zijn. Duurt 10 tot 20 seconden.",
    {"type": "object", "properties": {"vraag": {
        "type": "string", "description": "De vraag, volledig en op zichzelf te begrijpen, met plaats en datum als die ertoe doen."}},
     "required": ["vraag"]},
)


def willie_tool_list(face=None, web_search: bool = True) -> list[Tool]:
    """The fixed tool list (voice/tools.py) + show(), wrapped as D3 Tools. Tools run in a
    thread: rpicam-still takes seconds and the audio stream must keep flowing."""
    camera_jobs = 0

    async def call(name, args):
        nonlocal camera_jobs
        looking = None
        if face and name == "gezondheid":      # Brandstof tips/air take ~10 s
            looking = face.busy("BRANDSTOF...")
            looking.__enter__()
        if face and name == "kijk":
            camera_jobs += 1
            face.indicators(camera=True)
            looking = face.busy("LOOKING...", "seeing")
            looking.__enter__()
        worker = asyncio.create_task(asyncio.to_thread(willie_tools.call, name, args))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # A cancelled await cannot stop rpicam in its worker thread. Keep the
            # camera indicator on until the capture/upload actually finishes.
            try:
                await worker
            except Exception:
                pass
            raise
        finally:
            if looking and name == "gezondheid":
                looking.__exit__(None, None, None)
            elif looking:
                looking.__exit__(None, None, None)
                camera_jobs -= 1
                face.indicators(camera=camera_jobs > 0)
                face.set_state("thinking")           # photo sent; the answer is being made

    if face:
        from willie.face import picture

        # Show the camera's photo on the face the moment it is taken, while the model is
        # still looking at it: Wouter sees what WILL-E sees (21 Sep).
        def show_look(path):
            face.show_image(picture.from_file(path, "WAT IK ZIE"), seconds=15, flash=True)
        willie_tools.LOOK_HOOK = show_look
    wrapped = [
        Tool(d["name"], d["description"], d.get("parameters") or {"type": "object", "properties": {}},
             handler=lambda args, n=d["name"]: call(n, args))
        for d in willie_tools.declarations()
    ]
    screen = Tool(SHOW.name, SHOW.description, SHOW.parameters, handler=lambda args: show(args, face))
    photo = Tool(SHOW_PICTURE.name, SHOW_PICTURE.description, SHOW_PICTURE.parameters,
                 handler=lambda args: asyncio.to_thread(show_picture, args, face))
    tools = [screen, photo, *wrapped]
    if web_search:
        from willie.voice import search

        async def web_search_on_face(args):
            if not face:
                return await search.search(str(args.get("vraag", "")))
            with face.busy("RESEARCHING..."):
                return await search.search(str(args.get("vraag", "")))
        tools.append(Tool(WEB_SEARCH.name, WEB_SEARCH.description, WEB_SEARCH.parameters,
                          handler=web_search_on_face))
    return tools


def live_context() -> str:
    now = datetime.now()
    from willie import control
    body = control.context_line()          # mood + battery from the core (G2); "" if it is down
    from willie.skills import huis
    house = huis.context_line()            # L3: the home server's situation line; "" if it is gone
    return (f"Het is nu {DAYS[now.weekday()]} {now.day} {MONTHS[now.month - 1]} {now.year}, "
            f"{now:%H:%M}.{f' Jouw toestand: {body}.' if body else ''}"
            f"{f' Thuis nu: {house}' if house else ''}{willie_tools.remembered()}")


def configured_search() -> bool:
    """voice.search from the live willie.yaml. Off unless set: on a free-tier key the 3.x
    Live models refuse a setup with Google Search, and the model list would silently fall
    back to 2.5 (measured 21 Sep)."""
    try:
        from willie.config import Config
        return bool(Config().get("voice.search"))
    except Exception:
        return False


def configured_window() -> tuple[int, int] | None:
    """voice.window_trigger_tokens / voice.window_target_tokens (L4); trigger 0 = no cap."""
    try:
        from willie.config import Config
        cfg = Config()
        trigger, target = int(cfg.get("voice.window_trigger_tokens")), int(cfg.get("voice.window_target_tokens"))
    except Exception:
        trigger, target = 12_000, 8_000
    return (trigger, min(target, trigger - 1)) if trigger > 0 else None


def configured_lean() -> bool:
    """voice.lean_tools (L2): core tools in full, the rest behind `doe`. On unless switched off."""
    try:
        from willie.config import Config
        return bool(Config().get("voice.lean_tools"))
    except Exception:
        return True


def configured_language() -> str:
    """voice.language from the live willie.yaml; Dutch if the settings cannot be read."""
    try:
        from willie.config import Config
        return Config().get("voice.language")
    except Exception:
        return "nl"


NOT_FOR_ME = Tool(
    "niet_voor_mij",
    "Alleen als het DUIDELIJK niet voor jou is: Wouter praat al een paar zinnen met iemand "
    "anders in de kamer, zit aan de telefoon, of je hoort alleen tv of radio. Dan zeg je niets "
    "en roep je dit aan; het gesprek stopt en je luistert weer naar 'Hey Willie'. Twijfel je, "
    "of kan het een vraag of opmerking aan jou zijn (ook een nieuw onderwerp), dan is het "
    "wel voor jou en antwoord je gewoon.",
    {"type": "object", "properties": {"reden": {"type": "string", "description": "Een paar woorden, voor het logboek."}}},
)


def _chime(kind: str) -> None:
    try:
        from willie.audio import chime
        chime.play(kind)
    except Exception:                               # a missing ding must not end a conversation
        pass


def configured_followup() -> float:
    """voice.followup_s: how long he keeps listening without the wake word after the last
    words (his or Wouter's)."""
    try:
        from willie.config import Config
        return float(Config().get("voice.followup_s"))
    except Exception:
        return 20.0


def configured_standby() -> float:
    """voice.standby_s: after niet_voor_mij, how long he stays connected but silent."""
    try:
        from willie.config import Config
        return float(Config().get("voice.standby_s"))
    except Exception:
        return 60.0


def configured_clean() -> dict:
    """voice.clean / denoise_db / aec / aec_delay_ms / barge_in: the live uplink's cleaning."""
    values = {"clean": True, "denoise_db": -15, "aec": True, "aec_delay_ms": 295.0, "barge_in": 50.0}
    try:
        from willie.config import Config
        cfg = Config()
        for key in values:
            values[key] = type(values[key])(cfg.get(f"voice.{key}"))
    except Exception:
        pass
    return values


async def session(
    api_key: str,
    seconds: float | None = None,
    idle_timeout: float | None = None,
    on_event=None,
    face=None,
    cancel=None,
    recorder: subprocess.Popen | None = None,
    transcript: list | None = None,
    gate=None,
    resume: dict | None = None,
    extra_prompt: str = "",
    until=None,
    control: dict | None = None,
) -> str:
    """Hold a live conversation on the Pi's sound card. Returns the model name that worked.

    `idle_timeout` is the follow-up window (23 Sep: voice.followup_s, 20 s): the session
    closes that long after the last *words* - Wouter's as transcribed by the server, or the
    end of WILL-E's own audio. Noise does not keep it open. `cancel` (a threading.Event)
    ends it within half a second, mid-sentence: sleep mode from the phone app always wins
    (22 Sep, Wouter). `recorder` is the wake word's still-running arecord (one-breath
    questions). `transcript` gets (who, text) turns appended, "user"/"willie", for memory;
    a turn the model judged not for him (niet_voor_mij) is left out.

    Garage mode (K1) adds: `gate` (only Wouter's voice goes up; niet_voor_mij is not
    offered, the gate does that job), `resume` ({"handle": ...}: continue the conversation
    across reconnects; the newest handle is written back), `extra_prompt` (the workshop
    instructions), `until()` (checked every 2 s; False ends the session) and `control`
    (filled with "say": a thread-safe fn(text) that makes him say something in this
    conversation - a timer, a danger he saw).
    """
    from willie.face.runtime import Face
    owns_face = face is None
    face = Face.optional() if owns_face else face
    if face:
        face.set_state("connecting")

    def emit(kind, detail=""):
        if face:
            face.event(kind, detail)
        if on_event:
            on_event(kind, detail)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    ready = asyncio.Event()
    activity = [loop.time()]
    speaker = Speaker()
    native_search = configured_search()      # paid key: 3.8 searches itself, no zoek_op detour
    adapter = GeminiLiveAdapter(api_key, language=configured_language(), search=native_search,
                                resume=resume is not None, resume_handle=(resume or {}).get("handle", ""),
                                source="garage" if resume is not None else "robot")
    turns = transcript if transcript is not None else []
    # Face: "thinking" only once the server has heard words in this turn. The mic's own
    # level detector says when the turn *ends*; the transcription says it *was speech*.
    turn = {"heard": False, "ended": False}
    standby = Standby()
    standby_s = configured_standby()
    willie_tools.WRAP_UP.clear()
    # WRAP_UP: when first seen, when his answer after it made sound, when that turn ended.
    wrap = {"asked": 0.0, "audio": 0.0, "done": 0.0}

    def add_turn(who: str, text: str) -> None:
        if turns and turns[-1][0] == who:
            turns[-1] = (who, turns[-1][1] + text)
        else:
            turns.append((who, text))

    def audio(pcm: bytes) -> None:
        if standby.on:                             # he said he would stay quiet
            return
        starts_at = max(loop.time(), speaker.busy_until)
        speaker.write(pcm)
        if willie_tools.WRAP_UP.is_set():
            wrap["audio"] = loop.time()
        if face:
            face.audio(pcm, adapter.out_rate, starts_at=starts_at)
        activity[0] = loop.time()
        emit("audio", "")

    def event(kind: str, detail: str) -> None:
        if standby.on and kind in ("speaking", "turn_complete", "said", "heard",
                                   "user_speaking", "user_turn_end", "interrupted"):
            return
        if kind == "wake_again":
            activity[0] = loop.time()
            turn["heard"] = turn["ended"] = False
            threading.Thread(target=_chime, args=("idle",), daemon=True).start()
            if face:
                face.indicators(mic=True)
                face.set_state("listening")
            if on_event:
                on_event(kind, detail)
            return
        if kind == "interrupted":
            speaker.stop()
        elif kind == "closed":
            stop.set()
        elif kind == "tool":
            activity[0] = loop.time()
        elif kind == "heard":
            activity[0] = loop.time()
            add_turn("user", detail)
            turn["heard"] = True
            if turn["ended"]:                      # the words arrived after the quiet
                turn["ended"] = False
                emit("user_turn_end", "")
            if on_event:
                on_event(kind, detail)
            return
        elif kind == "said":
            add_turn("willie", detail)
            if on_event:
                on_event(kind, detail)
            return
        elif kind == "user_speaking":
            turn["heard"] = turn["ended"] = False
        elif kind == "user_turn_end":
            if not turn["heard"]:
                turn["ended"] = True               # wait for words before "thinking"
                return
            turn["heard"] = False
        elif kind in ("speaking", "turn_complete"):
            turn["heard"] = turn["ended"] = False
            if kind == "turn_complete" and willie_tools.WRAP_UP.is_set() and wrap["audio"]:
                wrap["done"] = loop.time()
        emit(kind, detail)

    def not_for_me(args: dict) -> dict:
        # Too eager on 23 Sep (Wouter): the first thing said after "Hey Willie" is always for
        # him, so the tool is refused until he has answered at least once.
        if not any(who == "willie" for who, _ in turns):
            return {"genegeerd": "Je bent net gewekt: dit is voor jou. Beantwoord het."}
        # Drop what he heard that was not meant for him, then stand by: connected, silent,
        # deaf to the room until "Hey Willie" (Standby).
        if turns and turns[-1][0] == "user":
            turns.pop()
        speaker.stop()
        standby.on, standby.since = True, loop.time()
        if face:
            face.set_state("idle", "SAY HEY WILLIE")
        emit("standby", f"not for me: {args.get('reden', '')}".strip())
        return {"ok": True, "opdracht": "Zeg niets. Je hoort pas weer iets als hij 'Hey Willie' zegt; "
                                        "ga dan gewoon verder met het gesprek."}

    adapter.on_audio(audio)
    adapter.on_event(event)
    if gate is not None:
        gate.on_event = lambda kind, detail="": event(kind, detail) if kind == "wake_again" else emit(kind, detail)
    # The mic runs from the start: whatever he hears while the session opens is kept.
    mic_task = asyncio.create_task(_microphone(adapter, speaker, stop, activity, event, ready, recorder,
                                               standby, gate))
    # zoek_op too when a free key exists: the free key cannot use the model's own search.
    tools = willie_tool_list(face, web_search=not native_search or bool(os.environ.get("GEMINI_API_KEY_FREE")))
    if gate is None:
        tools.append(Tool(NOT_FOR_ME.name, NOT_FOR_ME.description, NOT_FOR_ME.parameters, handler=not_for_me))
    if configured_lean():
        # L2: every turn pays for the whole setup, so only the core tools go in full.
        from willie.voice import lean
        tools = lean.lean(tools, lean.CORE + (NOT_FOR_ME.name,)
                          + (lean.GARAGE_CORE if resume is not None else ()))
    if control is not None:
        def say_in_session(text: str) -> None:
            asyncio.run_coroutine_threadsafe(adapter.send_text(text), loop)
        control["say"] = say_in_session
    prompt = system_prompt(LIVE_EXTRA) + (f"\n\n{extra_prompt}" if extra_prompt else "")
    try:
        await adapter.start_session(prompt, live_context(), tools)
    except BaseException:
        stop.set()
        mic_task.cancel()
        await asyncio.gather(mic_task, return_exceptions=True)
        speaker.stop()
        await adapter.close()
        if face and owns_face:
            face.close()
        raise
    activity[0] = loop.time()
    ready.set()

    async def idle_watch() -> None:
        next_until = loop.time() + 2
        while not stop.is_set():
            await asyncio.sleep(0.5)
            if cancel is not None and cancel.is_set():
                emit("idle", "sleep mode")
                stop.set()
                break
            if until is not None and loop.time() >= next_until:
                next_until = loop.time() + 2
                if not await asyncio.to_thread(until):
                    emit("idle", "mode ended")
                    stop.set()
                    break
            if standby.on:
                if loop.time() - standby.since >= standby_s:
                    emit("idle", f"{standby_s:.0f} s standby without 'Hey Willie'")
                    stop.set()
                continue
            if willie_tools.WRAP_UP.is_set():
                now = loop.time()
                wrap["asked"] = wrap["asked"] or now
                # End once his answer after the tool call has played out (12 s at most).
                if (wrap["done"] and not speaker.speaking(now)) or now - wrap["asked"] > WRAP_UP_MAX_S:
                    emit("idle", "wrap-up asked by a tool")
                    stop.set()
                    break
                continue
            # The window starts when his voice has finished playing, not when it arrived.
            # While a card, picture or pinout is on the screen Wouter is reading it: the
            # follow-up window only starts when it is gone (25 Sep: he went back to sleep
            # under a pinout and then ignored the next question).
            if face and getattr(face, "showing", lambda: False)():
                activity[0] = loop.time()
            last = max(activity[0], speaker.busy_until)
            if idle_timeout and loop.time() - last >= idle_timeout:
                emit("idle", f"{idle_timeout:.0f} s without words")
                stop.set()

    tasks = [mic_task, asyncio.create_task(idle_watch())]
    try:
        waiter = asyncio.create_task(stop.wait())
        await asyncio.wait([waiter, *tasks], timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        speaker.stop()
        await adapter.close()
        if control is not None:
            control.pop("say", None)
        if resume is not None:
            resume["handle"] = adapter.resume_handle
        if face and owns_face:
            face.close()
        # A task that dies on its own leaves the session looking like a clean
        # exit, so say which one and why.
        for name, result in zip(("microphone", "idle"), results):
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                emit("error", f"{name}: {type(result).__name__}: {result}")
    return adapter.model
