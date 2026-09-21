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

import array
import asyncio
import base64
import json
import logging
import os
import subprocess
from datetime import datetime

import websockets

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
                 search: bool = False):
        """`model` = one entry of MODELS (or any Live model; empty = try MODELS in order).
        `url` replaces the Google endpoint - the tests point it at a local mock server."""
        super().__init__()
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
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

    # ---- lifecycle -------------------------------------------------------------
    async def start_session(self, persona: str, context: str, tools: list[Tool]) -> None:
        if not self.api_key and not self.url:
            raise RuntimeError("GEMINI_API_KEY missing")
        self._register_tools(tools)
        rule = LANGUAGES.get(self.language, ("", ""))[1]
        prompt = "\n\n".join(p for p in (persona, rule, context) if p).strip()
        last_error = "no model accepted the session"
        for model, shape in self.models:
            url = self.url or f"wss://{HOST}{PATH}?key={self.api_key}"
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
            self.is_open, self._closing = True, False
            self._receiver = asyncio.create_task(self._receive())
            await self._emit("ready", model)
            return
        raise RuntimeError(last_error)

    def _setup_message(self, model: str, prompt: str) -> dict:
        setup = {
            "model": model,
            # Ask the server to be slow to call something an interruption. The
            # runner's echo gate does the heavy lifting, but a deaf-er detector
            # helps for whatever leaks through.
            "realtimeInputConfig": {
                "automaticActivityDetection": {
                    "startOfSpeechSensitivity": "START_SENSITIVITY_LOW",
                    "endOfSpeechSensitivity": "END_SENSITIVITY_LOW",
                    "prefixPaddingMs": 300,
                    "silenceDurationMs": 800,
                }
            },
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
        if self.tools:
            tools.append({"functionDeclarations": [t.declaration() for t in self.tools.values()]})
        if self.search:
            # Free on the free tier; paid: 5,000 searches/month free, then $14 per 1,000 (21 Sep).
            tools.append({"googleSearch": {}})
        if tools:
            setup["tools"] = tools
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

    async def interrupt(self) -> None:
        """The Live API has no client-side "cancel": the server keeps generating the turn it
        started. So the rest of that turn is dropped here, until its turnComplete (or the
        server's own interruption) arrives."""
        if self._speaking:
            self._speaking = False
            self._dropping = True
            await self._emit("interrupted")

    async def close(self) -> None:
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
            await self._emit("closed", reason)

    async def _handle(self, message: dict) -> None:
        call = _get(message, "toolCall", "tool_call")
        if call:
            # Each call runs as its own task: the camera takes seconds and the
            # audio must keep flowing (and barge-in keep working) meanwhile.
            for function in _get(call, "functionCalls", "function_calls") or []:
                asyncio.create_task(self._answer_tool(function))
            return
        if _get(message, "goAway", "go_away"):
            await self._emit("error", "server will end the session soon (goAway)")
            return
        server = _get(message, "serverContent", "server_content") or {}
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
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    out = array.array("h")
    for value in samples:
        for _ in range(factor):
            out.append(value)
    return out.tobytes()


class Speaker:
    """One long-lived aplay process, so words do not get a gap between chunks."""

    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        # When the audio written so far will have finished playing. aplay buffers,
        # so this is tracked from the byte count rather than from the last write.
        self.busy_until = 0.0

    def speaking(self, now: float) -> bool:
        return now < self.busy_until + BARGE_IN_TAIL

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
                data = _upsample(speech.scale(pcm))
                seconds = len(data) / (CARD_RATE * 2)
                loop = asyncio.get_event_loop()
                self.busy_until = max(self.busy_until, loop.time()) + seconds
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


# The live mic uses the same capture as the wake word (willie/audio/mic.py): left channel
# only, x voice.wake_gain. Before 21 Sep it captured mono, which halved the INMP441's
# already quiet signal: Wouter's voice read 2-3 % FS, below the old fixed 5 % speech
# threshold, so the face never saw him talk (no listening -> thinking switch).
#
# Speech vs room is now relative: the room level is the 20th percentile of the last ~5 s
# of chunk peaks, and a chunk counts as speech when it is well above that.
SPEECH_OVER_ROOM = 2.5
SPEECH_MIN = 0.10            # never call anything under 10 % FS (after gain) speech
TURN_END_S = 0.6             # this much quiet after speech = his turn is over -> thinking

# The microphone hears the speaker - there is no echo cancellation and they sit on the
# same board. While he is talking the uplink is gated, or the server's voice detector
# hears WILL-E himself and cuts him off. Barge-in only gets through above this level;
# with the x8 gain his own echo clips, so it is off by default (D4: not a priority,
# needs AEC). Set WILLIE_BARGE_IN_LEVEL (0-1) to experiment.
BARGE_IN_FS = float(os.environ.get("WILLIE_BARGE_IN_LEVEL", "2.0"))
# Keep the gate shut a moment after the audio ends, for the tail out of the cone.
BARGE_IN_TAIL = 0.4


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


async def _microphone(adapter: VoiceAdapter, speaker: Speaker, stop: asyncio.Event,
                      activity: list[float], on_event) -> None:
    from willie.audio import mic
    frames = IN_RATE * CHUNK_MS // 1000
    factor = mic.gain()
    detector = SpeechDetector()
    sent, loudest = 0, 0.0
    user_speaking, last_speech = False, 0.0
    process = await asyncio.create_subprocess_exec(
        *mic.command(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    loop = asyncio.get_running_loop()
    on_event("mic_active", "")
    try:
        # The first ~0.8 s after arecord opens is the mic settling (a loud decaying bump).
        await process.stdout.readexactly(int(IN_RATE * 0.8) * mic.FRAME)
        while not stop.is_set():
            try:
                raw = await process.stdout.readexactly(frames * mic.FRAME)
            except asyncio.IncompleteReadError:
                break
            chunk = mic.left(raw, factor)
            peak = _peak(chunk)
            # While he is speaking, swallow anything that is not clearly louder
            # than his own voice coming back through the microphone.
            if speaker.speaking(loop.time()) and peak < BARGE_IN_FS:
                continue
            await adapter.send_audio(chunk)
            sent += 1
            loudest = max(loudest, peak)
            if detector.is_speech(peak):
                activity[0] = loop.time()
                last_speech = loop.time()
                if not user_speaking:
                    on_event("user_speaking", "")
                    user_speaking = True
            elif user_speaking and loop.time() - last_speech >= TURN_END_S:
                on_event("user_turn_end", "")
                user_speaking = False
            if sent % 20 == 0:                       # every 2 s
                on_event("uplink", f"{sent} chunks, loudest {loudest * 100:.1f}% FS, "
                                   f"room {detector.room() * 100:.1f}%")
                loudest = 0.0
    finally:
        on_event("mic_idle", "")
        if process.returncode is None:
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
            if looking:
                looking.__exit__(None, None, None)
                camera_jobs -= 1
                face.indicators(camera=camera_jobs > 0)
                face.set_state("thinking")           # photo sent; the answer is being made

    if face:
        from willie.face import picture

        # Show the camera's photo on the face the moment it is taken, while the model is
        # still looking at it: Wouter sees what WILL-E sees (21 Sep).
        def show_look(path):
            face.show_image(picture.from_file(path, "WAT IK ZIE"), seconds=15)
        willie_tools.LOOK_HOOK = show_look
    wrapped = [
        Tool(d["name"], d["description"], d.get("parameters") or {"type": "object", "properties": {}},
             handler=lambda args, n=d["name"]: call(n, args))
        for d in willie_tools.DECLARATIONS
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
    return (f"Het is nu {DAYS[now.weekday()]} {now.day} {MONTHS[now.month - 1]} {now.year}, "
            f"{now:%H:%M}.{willie_tools.remembered()}")


def configured_search() -> bool:
    """voice.search from the live willie.yaml. Off unless set: on a free-tier key the 3.x
    Live models refuse a setup with Google Search, and the model list would silently fall
    back to 2.5 (measured 21 Sep)."""
    try:
        from willie.config import Config
        return bool(Config().get("voice.search"))
    except Exception:
        return False


def configured_language() -> str:
    """voice.language from the live willie.yaml; Dutch if the settings cannot be read."""
    try:
        from willie.config import Config
        return Config().get("voice.language")
    except Exception:
        return "nl"


async def session(
    api_key: str,
    seconds: float | None = None,
    idle_timeout: float | None = None,
    on_event=None,
    face=None,
) -> str:
    """Hold a live conversation on the Pi's sound card. Returns the model name that worked.

    `idle_timeout` closes the session after that many seconds with neither side
    making a sound, which is what the wake-word loop uses to go back to sleep.
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
    activity = [loop.time()]
    speaker = Speaker()
    native_search = configured_search()      # paid key: 3.8 searches itself, no zoek_op detour
    adapter = GeminiLiveAdapter(api_key, language=configured_language(), search=native_search)

    def audio(pcm: bytes) -> None:
        starts_at = max(loop.time(), speaker.busy_until)
        speaker.write(pcm)
        if face:
            face.audio(pcm, adapter.out_rate, starts_at=starts_at)
        activity[0] = loop.time()
        emit("audio", "")

    def event(kind: str, detail: str) -> None:
        if kind == "interrupted":
            speaker.stop()
        elif kind == "closed":
            stop.set()
        elif kind == "tool":
            activity[0] = loop.time()
        emit(kind, detail)

    adapter.on_audio(audio)
    adapter.on_event(event)
    try:
        await adapter.start_session(system_prompt(LIVE_EXTRA), live_context(), willie_tool_list(face, web_search=not native_search))
    except BaseException:
        speaker.stop()
        await adapter.close()
        if face and owns_face:
            face.close()
        raise

    async def idle_watch() -> None:
        while not stop.is_set():
            await asyncio.sleep(0.5)
            if idle_timeout and loop.time() - activity[0] >= idle_timeout:
                emit("idle", f"{idle_timeout:.0f} s without speech")
                stop.set()

    tasks = [asyncio.create_task(_microphone(adapter, speaker, stop, activity, emit)),
             asyncio.create_task(idle_watch())]
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
        if face and owns_face:
            face.close()
        # A task that dies on its own leaves the session looking like a clean
        # exit, so say which one and why.
        for name, result in zip(("microphone", "idle"), results):
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                emit("error", f"{name}: {type(result).__name__}: {result}")
    return adapter.model
