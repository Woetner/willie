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


def listen_for_wake(stop_after: float | None = None, on_tick=None) -> bool:
    """Block until the wake word is heard. True on a detection, False on timeout.

    The microphone is released before returning, so the live session that
    follows can open it. `on_tick(elapsed, probability)` is called about every
    half second with the highest probability seen, for bench tuning.
    """
    if not available():
        raise RuntimeError("pymicro-wakeword missing - make deps")
    model, features = _load()
    model.reset()
    features.reset()
    factor = mic.gain()
    recorder = subprocess.Popen(mic.command(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    elapsed, loudest, next_tick = 0.0, 0.0, 0.5
    try:
        while True:
            raw = recorder.stdout.read(CHUNK * mic.FRAME) if recorder.stdout else b""
            if len(raw) < CHUNK * mic.FRAME:
                return False
            chunk = mic.left(raw, factor)
            elapsed += CHUNK / mic.RATE
            for frame in features.process_streaming(chunk):
                probability = model.process_streaming_prob(frame)
                if probability is None:
                    continue
                loudest = max(loudest, probability)
                if probability > model.probability_cutoff:
                    return True
            if on_tick and elapsed >= next_tick:
                on_tick(elapsed, loudest)
                loudest, next_tick = 0.0, next_tick + 0.5
            if stop_after and elapsed >= stop_after:
                return False
    finally:
        recorder.terminate()
        recorder.wait(timeout=2)
