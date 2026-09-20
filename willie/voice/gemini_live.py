"""Speech-to-speech with the Gemini Live API (bidi WebSocket).

Unlike the request/response path in `tools/ask_camera.py`, this keeps a session
open: microphone audio streams up continuously, the model's own voice streams
back, and either side can interrupt the other. That is what makes it feel like
talking rather than waiting.

Audio contract:
  up    16 kHz 16-bit mono PCM, captured by ALSA (its resampler is clean; the
        hand-rolled decimation tried on 20 Sep smeared consonants badly enough
        that the model could not understand Dutch at all)
  down  24 kHz 16-bit mono PCM, upsampled to 48 kHz before aplay, because this
        card plays 48 kHz and is silent at lower rates (B6/B7 bench finding)

This is a bench prototype of the Gate G1 adapter (D4), not the adapter itself:
no tool calls, no face states, no mood. It exists to measure whether the Pi 3 A+
can hold a live session at all (G2) and how it feels in Dutch.
"""

from __future__ import annotations

import array
import asyncio
import base64
import json
import os
import subprocess

import websockets

from willie.audio import speech

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
VOICE = os.environ.get("WILLIE_LIVE_VOICE", "Puck")

IN_RATE, OUT_RATE, CARD_RATE = 16_000, 24_000, 48_000
CHUNK_MS = 100
DEVICE = "plughw:CARD=sndrpigooglevoi,DEV=0"

SYSTEM_PROMPT = (
    "Je bent WILL-E, een nieuwsgierige werkplaatsrobot van Wouter. "
    "Je praat Nederlands, kort en spontaan, hooguit twee zinnen per beurt. "
    "Je bent geen assistent maar een maatje: nuchter, een beetje grappig, "
    "en je zegt het gewoon als je iets niet weet."
)


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
                self.process.stdin.write(_upsample(speech.scale(pcm)))
                self.process.stdin.flush()
        except (BrokenPipeError, ValueError):
            self.process = None

    def stop(self) -> None:
        """Cut playback dead - used when the model is interrupted (barge-in)."""
        if self.process and self.process.poll() is None:
            self.process.kill()
        self.process = None


async def _setup(socket, model: str) -> None:
    await socket.send(json.dumps({
        "setup": {
            "model": model,
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": VOICE}}},
            },
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        }
    }))


def _uplink(chunk: bytes, shape: str) -> str:
    blob = {"mimeType": f"audio/pcm;rate={IN_RATE}", "data": base64.b64encode(chunk).decode("ascii")}
    inner = {"audio": blob} if shape == "audio" else {"mediaChunks": [blob]}
    return json.dumps({"realtimeInput": inner})


# Anything below this is room tone, measured on the bench: a quiet room reads
# about 1.5 % FS and speech at 30 cm reads 20-40 %.
SPEECH_FS = 0.05


async def _send_microphone(socket, stop: asyncio.Event, shape: str, on_event=None, activity=None) -> None:
    frames = IN_RATE * CHUNK_MS // 1000
    sent = 0
    loudest = 0.0
    process = await asyncio.create_subprocess_exec(
        "arecord", "-D", DEVICE, "-f", "S16_LE", "-r", str(IN_RATE), "-c", "1", "-t", "raw", "-q",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        while not stop.is_set():
            chunk = await process.stdout.read(frames * 2)
            if not chunk:
                break
            await socket.send(_uplink(chunk, shape))
            sent += 1
            samples = array.array("h")
            samples.frombytes(chunk[: len(chunk) // 2 * 2])
            if samples:
                peak = max(max(samples), -min(samples)) / 32768
                loudest = max(loudest, peak)
                if peak >= SPEECH_FS and activity is not None:
                    activity[0] = asyncio.get_running_loop().time()
            if on_event and sent % 20 == 0:      # every 2 s
                on_event("uplink", f"{sent} chunks, loudest {loudest * 100:.1f}% FS")
                loudest = 0.0
    finally:
        process.terminate()
        await process.wait()


async def _receive(socket, speaker: Speaker, stop: asyncio.Event, on_event=None, activity=None) -> None:
    async for raw in socket:
        if stop.is_set():
            break
        message = json.loads(raw) if isinstance(raw, (str, bytes)) else {}
        server = message.get("serverContent") or message.get("server_content") or {}
        if server.get("interrupted"):
            speaker.stop()
            if on_event:
                on_event("interrupted", "")
            continue
        turn = server.get("modelTurn") or server.get("model_turn") or {}
        for part in turn.get("parts", []):
            blob = part.get("inlineData") or part.get("inline_data")
            if blob and blob.get("data"):
                speaker.write(base64.b64decode(blob["data"]))
                if activity is not None:
                    activity[0] = asyncio.get_running_loop().time()
                if on_event:
                    on_event("audio", "")
            if part.get("text") and on_event:
                on_event("text", part["text"])
        if (server.get("turnComplete") or server.get("turn_complete")) and on_event:
            on_event("turn_complete", "")


async def _watch_idle(stop: asyncio.Event, activity: list[float], idle_timeout: float, on_event=None) -> None:
    """End the session once nobody has spoken and nothing has been said back."""
    loop = asyncio.get_running_loop()
    activity[0] = loop.time()
    while not stop.is_set():
        await asyncio.sleep(0.5)
        if loop.time() - activity[0] >= idle_timeout:
            if on_event:
                on_event("idle", f"{idle_timeout:.0f} s without speech")
            stop.set()
            return


async def session(
    api_key: str,
    seconds: float | None = None,
    idle_timeout: float | None = None,
    on_event=None,
) -> str:
    """Hold a live conversation. Returns the model name that worked.

    `idle_timeout` closes the session after that many seconds with neither side
    making a sound, which is what the wake-word loop uses to go back to sleep.
    """
    stop = asyncio.Event()
    activity = [0.0]
    speaker = Speaker()
    last_error = "no model accepted the session"
    for model, shape in MODELS:
        url = f"wss://{HOST}{PATH}?key={api_key}"
        try:
            async with websockets.connect(url, max_size=None, ping_interval=20) as socket:
                await _setup(socket, model)
                ack = json.loads(await asyncio.wait_for(socket.recv(), timeout=15))
                if "setupComplete" not in ack and "setup_complete" not in ack:
                    last_error = f"{model}: {json.dumps(ack)[:160]}"
                    continue
                if on_event:
                    on_event("ready", model)
                tasks = [
                    asyncio.create_task(_send_microphone(socket, stop, shape, on_event, activity)),
                    asyncio.create_task(_receive(socket, speaker, stop, on_event, activity)),
                ]
                if idle_timeout:
                    tasks.append(asyncio.create_task(_watch_idle(stop, activity, idle_timeout, on_event)))
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
                    # A task that dies on its own leaves the session looking like
                    # a clean exit, so say which one and why.
                    for name, result in zip(("microphone", "receive", "idle"), results):
                        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                            if on_event:
                                on_event("error", f"{name}: {type(result).__name__}: {result}")
                return model
        except asyncio.TimeoutError:
            last_error = f"{model}: no setup answer in 15 s"
        except (websockets.WebSocketException, OSError) as exc:
            last_error = f"{model}: {exc}"
    raise RuntimeError(last_error)
