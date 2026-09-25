"""Wake word "Hey Willie" on the Pi (D2, D19), spotted with microWakeWord.

The model is trained on the Mac from synthetic English and Dutch voices
(tools/wakeword/, see config/wakewords/README.md) and runs here through
`pymicro-wakeword`, which ships its own small TFLite C library: no TensorFlow,
no onnxruntime. Measured with a stock model on the Pi (21 Sep): 32 MB RSS and
5 % of one core while listening - Vosk, the bench detector it replaces, was
150 MB and 20 %.

Until hey_willie.tflite exists, the stock "hey jarvis" model stands in so the
loop can be tested; `describe()` says which one is listening.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from willie.audio import mic

CHUNK = 1024            # frames = 64 ms at 16 kHz; features come out every 10 ms
MODEL = Path(__file__).resolve().parents[2] / "config" / "wakewords" / "hey_willie.json"

_detector = None


def available() -> bool:
    try:
        import pymicro_wakeword  # noqa: F401
    except ImportError:
        return False
    return True


def describe() -> str:
    return '"hey willie"' if MODEL.exists() else '"hey jarvis" (stand-in: hey_willie.json not trained yet)'


def _load():
    global _detector
    if _detector is None:
        from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures, Model
        model = MicroWakeWord.from_config(MODEL) if MODEL.exists() else MicroWakeWord.from_builtin(Model.HEY_JARVIS)
        _detector = (model, MicroWakeWordFeatures())
    return _detector


def cutoff() -> float:
    """voice.wake_sensitivity -> the model's probability cutoff (cutoff = 1 - sensitivity).
    Before 25 Sep the setting was not wired and the JSON's 0.7 ruled. On Wouter's clips
    64/67 fire at any cutoff 0.3-0.9 and his talk + TV peaks at 0.46, so 0.5 costs nothing
    there and helps soft or far "Hey Willie"s (bench, 25 Sep; the 1-hour TV test decides)."""
    try:
        from willie.config import Config
        return 1.0 - float(Config().get("voice.wake_sensitivity"))
    except Exception:
        return 0.5


def listen_for_wake(stop_after: float | None = None, on_tick=None, stop=None) -> bool:
    """Block until the wake word is heard. True on a detection, False on timeout.
    The microphone is released before returning."""
    recorder = wait_for_wake(stop_after, on_tick, stop)
    if recorder is None:
        return False
    recorder.terminate()
    recorder.wait(timeout=2)
    return True


# fn(chunk) called with every mic chunk while waiting for the wake word (K5 sentry: sound
# triggers without a second recorder on the one mic).
LEVEL_HOOK = None


def wait_for_wake(stop_after: float | None = None, on_tick=None, stop=None) -> subprocess.Popen | None:
    """Block until the wake word is heard. On a detection the arecord process is returned
    still running (23 Sep), so the live session keeps listening without a gap and hears
    the question said straight after "Hey Willie". None on timeout (mic released). `on_tick(elapsed, probability)` is called about every
    half second with the highest probability seen, for bench tuning. `stop` (a
    threading.Event) ends the wait early, e.g. when the phone app puts him to sleep.
    """
    if not available():
        raise RuntimeError("pymicro-wakeword missing - make deps")
    model, features = _load()
    threshold = cutoff()
    model.reset()
    features.reset()
    stream = mic.Stream(mic.gain())
    recorder = subprocess.Popen(mic.command(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    elapsed, loudest, next_tick = 0.0, 0.0, 0.5
    detected = False
    try:
        while True:
            raw = recorder.stdout.read(CHUNK * mic.FRAME) if recorder.stdout else b""
            if len(raw) < CHUNK * mic.FRAME:
                return None
            chunk = stream.convert(raw)
            if LEVEL_HOOK is not None:
                try:
                    LEVEL_HOOK(chunk)            # sentry mode (K5) hears the room through this
                except Exception:
                    pass
            elapsed += CHUNK / mic.RATE
            for frame in features.process_streaming(chunk):
                probability = model.process_streaming_prob(frame)
                if probability is None:
                    continue
                loudest = max(loudest, probability)
                if probability > threshold:
                    detected = True
                    return recorder
            if on_tick and elapsed >= next_tick:
                on_tick(elapsed, loudest)
                loudest, next_tick = 0.0, next_tick + 0.5
            if stop_after and elapsed >= stop_after:
                return None
            if stop is not None and stop.is_set():
                return None
    finally:
        if not detected:
            recorder.terminate()
            recorder.wait(timeout=2)


class Spotter:
    """The wake word on audio someone else already captures: the live session feeds it its
    own mic chunks (16 kHz mono S16, gain applied) while it stands by (23 Sep), so "Hey
    Willie" wakes him again without closing and reopening the connection. Shares the loaded
    model with wait_for_wake(); both never run at the same time."""

    def __init__(self) -> None:
        self.model, self.features = _load()
        self.threshold = cutoff()
        self.reset()

    def reset(self) -> None:
        self.model.reset()
        self.features.reset()

    def feed(self, chunk: bytes) -> bool:
        for frame in self.features.process_streaming(chunk):
            probability = self.model.process_streaming_prob(frame)
            if probability is not None and probability > self.threshold:
                self.reset()
                return True
        return False
