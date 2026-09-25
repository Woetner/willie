"""Cleaning the live uplink: echo cancellation, noise suppression and automatic gain (25 Sep).

Why: the fixed mic gain (voice.wake_gain, x16) clips a voice at 30 cm and still leaves one
at 3 m quiet, and without echo cancellation the mic hears WILL-E himself, so the uplink had
to be shut while he talks (no barge-in, D4). speexdsp does all three in a few % of a core
and ~1 MB: its echo canceller (MDF) subtracts what the speaker played, its preprocessor
removes steady noise and levels the voice.

Loaded with ctypes from the Debian package libspeexdsp1 (tools/pi_setup.sh), so nothing
is compiled. Without the library `Cleaner.load()` returns None and the runner falls back
to the old path (fixed gain + echo gate).

Only the live session uses this. The wake word keeps the plain fixed-gain signal it was
trained on (willie/audio/mic.py); retrain it on cleaned audio before switching that too.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging

import numpy as np

log = logging.getLogger("willie.audio.clean")

RATE = 16_000
FRAME = 160                     # 10 ms, speex's sweet spot
TAIL = 4_096                    # 256 ms of echo path: the board plus a small room
# Level into speex. The cleaner runs on its own, low gain: the x16 wake gain clips
# WILL-E's own voice in the mic, and a clipped echo cannot be cancelled (it is no longer
# a linear copy of what was played). The AGC brings the voice up afterwards.
IN_GAIN = 2.0
AGC_LEVEL = 8_000.0             # speech target, ~25 % FS
AGC_MAX_GAIN_DB = 30

# speex_preprocess_ctl / speex_echo_ctl requests (speex/speex_preprocess.h, speex_echo.h)
_SET_DENOISE, _SET_AGC, _SET_AGC_LEVEL, _SET_DEREVERB = 0, 2, 6, 8
_SET_NOISE_SUPPRESS, _SET_ECHO_STATE, _SET_AGC_MAX_GAIN = 18, 24, 30
_ECHO_SET_SAMPLING_RATE = 24

_lib = None
_shared = None


def library():
    """libspeexdsp, or None when it is not installed (the Mac, a fresh Pi)."""
    global _lib
    if _lib is None:
        name = ctypes.util.find_library("speexdsp") or "libspeexdsp.so.1"
        try:
            lib = ctypes.CDLL(name)
        except OSError:
            _lib = False
            return None
        lib.speex_preprocess_state_init.restype = ctypes.c_void_p
        lib.speex_preprocess_state_init.argtypes = [ctypes.c_int, ctypes.c_int]
        lib.speex_preprocess_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        lib.speex_preprocess_run.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        lib.speex_preprocess_state_destroy.argtypes = [ctypes.c_void_p]
        lib.speex_echo_state_init.restype = ctypes.c_void_p
        lib.speex_echo_state_init.argtypes = [ctypes.c_int, ctypes.c_int]
        lib.speex_echo_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        lib.speex_echo_cancellation.argtypes = [ctypes.c_void_p] * 4
        lib.speex_echo_state_destroy.argtypes = [ctypes.c_void_p]
        _lib = lib
    return _lib or None


class Cleaner:
    """16 kHz mono S16 in (at IN_GAIN), cleaned 16 kHz mono S16 out. Feed whole 10 ms frames."""

    def __init__(self, lib, denoise_db: int, echo: bool) -> None:
        self.lib = lib
        self.pre = lib.speex_preprocess_state_init(FRAME, RATE)
        self.echo = lib.speex_echo_state_init(FRAME, TAIL) if echo else None
        on = ctypes.c_int(1)
        lib.speex_preprocess_ctl(self.pre, _SET_DENOISE, ctypes.byref(on))
        lib.speex_preprocess_ctl(self.pre, _SET_NOISE_SUPPRESS, ctypes.byref(ctypes.c_int(int(denoise_db))))
        lib.speex_preprocess_ctl(self.pre, _SET_AGC, ctypes.byref(on))
        lib.speex_preprocess_ctl(self.pre, _SET_AGC_LEVEL, ctypes.byref(ctypes.c_float(AGC_LEVEL)))
        lib.speex_preprocess_ctl(self.pre, _SET_AGC_MAX_GAIN, ctypes.byref(ctypes.c_int(AGC_MAX_GAIN_DB)))
        if self.echo:
            lib.speex_echo_ctl(self.echo, _ECHO_SET_SAMPLING_RATE, ctypes.byref(ctypes.c_int(RATE)))
            # The preprocessor also removes what the echo canceller leaves behind.
            lib.speex_preprocess_ctl(self.pre, _SET_ECHO_STATE, self.echo)

    @classmethod
    def load(cls, denoise_db: int = -15, echo: bool = True) -> "Cleaner | None":
        lib = library()
        if lib is None:
            log.warning("libspeexdsp not found: live mic uncleaned (sudo apt install libspeexdsp1)")
            return None
        return cls(lib, denoise_db, echo)

    @classmethod
    def shared(cls, denoise_db: int, echo: bool) -> "Cleaner | None":
        """One cleaner for all conversations of this process. A fresh echo filter needs about
        a second of his speech to adapt (bench, 25 Sep), so the first words of every answer
        would leak; the speaker-to-mic path barely changes, so the adapted filter is kept."""
        global _shared
        key = (int(denoise_db), bool(echo))
        if _shared is None or _shared[0] != key:
            if _shared is not None and _shared[1] is not None:
                _shared[1].close()
            _shared = (key, cls.load(*key))
        return _shared[1]

    def process(self, mic: bytes, played: np.ndarray | None = None) -> bytes:
        """`played` = what the speaker played in the same 16 kHz samples (EchoReference)."""
        samples = np.frombuffer(mic, "<i2")
        samples = samples[: len(samples) // FRAME * FRAME].copy()
        out = np.empty_like(samples)
        if self.echo is not None and played is None:
            played = np.zeros(len(samples), np.int16)
        for start in range(0, len(samples), FRAME):
            frame = samples[start:start + FRAME]
            if self.echo is not None:
                ref = np.ascontiguousarray(played[start:start + FRAME], np.int16)
                result = out[start:start + FRAME]
                self.lib.speex_echo_cancellation(self.echo, frame.ctypes.data, ref.ctypes.data,
                                                 result.ctypes.data)
                frame = result
            else:
                out[start:start + FRAME] = frame
                frame = out[start:start + FRAME]
            self.lib.speex_preprocess_run(self.pre, frame.ctypes.data)
        return out.tobytes()

    def close(self) -> None:
        if self.pre:
            self.lib.speex_preprocess_state_destroy(self.pre)
            self.pre = None
        if self.echo:
            self.lib.speex_echo_state_destroy(self.echo)
            self.echo = None


class EchoReference:
    """What the speaker played, on the event loop's clock, at 16 kHz: the echo canceller's
    reference. The speaker writes blocks with the time they will start sounding; the mic
    reads back the samples for the time its chunk was captured. Both run on the one asyncio
    loop, so no lock. `delay` is the card's playback latency minus its capture latency:
    it starts at voice.aec_delay_ms (measured with make bench-aec) and `Aligner` keeps it
    right, because it is not fixed - when the model's audio arrives slower than real time
    aplay runs dry and restarts, and the true delay wanders by ±200 ms (bench, 25 Sep)."""

    SECONDS = 30

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.size = RATE * self.SECONDS
        self.buffer = np.zeros(self.size, np.int16)
        self.until = 0              # absolute sample index: nothing valid at or after this

    def _put(self, start: int, samples: np.ndarray) -> None:
        index = (start + np.arange(len(samples))) % self.size
        self.buffer[index] = samples

    def play(self, pcm: bytes, rate: int, at: float) -> None:
        """`pcm` (S16 mono at `rate`) starts coming out of the speaker at loop time `at`."""
        samples = np.frombuffer(pcm[: len(pcm) // 2 * 2], "<i2").astype(np.float32)
        if not len(samples):
            return
        count = int(len(samples) * RATE / rate)
        resampled = np.interp(np.arange(count) * (rate / RATE), np.arange(len(samples)), samples)
        start = round(at * RATE)
        if start > self.until:                   # silence since the last block
            gap = min(start - self.until, self.size)
            self._put(start - gap, np.zeros(gap, np.int16))
        self._put(start, resampled.astype(np.int16))
        self.until = max(self.until, start + count)

    def cut(self, at: float) -> None:
        """Playback was killed at `at`: whatever was queued after it never sounded."""
        self.until = min(self.until, round(at * RATE))

    def read(self, at: float, count: int) -> np.ndarray:
        start = round((at - self.delay) * RATE)
        index = start + np.arange(count)
        valid = (index < self.until) & (index >= self.until - self.size)
        return np.where(valid, self.buffer[index % self.size], 0).astype(np.int16)


def lag(heard: np.ndarray, played: np.ndarray, before: float = 0.2, after: float = 0.6) -> tuple[float, float]:
    """How many seconds `heard` runs behind `played` (-before..+after), and how clearly:
    the correlation peak over the median. GCC-PHAT: the cross-spectrum is whitened and
    limited to 200-4000 Hz, so speech gives one sharp peak instead of a broad hump
    (plain correlation on speech rarely got past 8x on the bench, 25 Sep)."""
    size = 1 << (len(heard) + len(played)).bit_length()
    cross = np.fft.rfft(heard, size) * np.conj(np.fft.rfft(played, size))
    band = np.fft.rfftfreq(size, 1 / RATE)
    cross = np.where((band >= 200) & (band <= 4000), cross / (np.abs(cross) + 1e-9), 0)
    corr = np.abs(np.fft.irfft(cross, size))
    lags = np.concatenate((np.arange(0, int(after * RATE)), np.arange(-int(before * RATE), 0)))
    values = corr[lags]
    best = int(np.argmax(values))
    return lags[best] / RATE, float(values[best] / (np.median(values) + 1e-9))


class Aligner:
    """Keeps EchoReference.delay on the real speaker-to-mic delay while he talks: every
    half second, the last second of mic and reference are cross-correlated and a clear
    offset is added to the delay. ~1 ms of CPU per check."""

    WINDOW = RATE                # 1 s
    EVERY = RATE // 2
    CLEAR = 12.0                 # peak/median needed to trust a lag
    AGREE = 0.01                 # two trusted lags in a row within 10 ms before moving
    LOUD = 300.0                 # reference rms below this: he is (nearly) silent, skip

    def __init__(self, reference: EchoReference) -> None:
        self.reference = reference
        self.heard: list[np.ndarray] = []
        self.played: list[np.ndarray] = []
        self.count = self.since = 0
        self.candidate: float | None = None

    def feed(self, heard: np.ndarray, played: np.ndarray) -> None:
        self.heard.append(np.asarray(heard, np.float32))
        self.played.append(np.asarray(played, np.float32))
        self.count += len(heard)
        self.since += len(heard)
        while self.count - len(self.heard[0]) >= self.WINDOW:
            self.count -= len(self.heard.pop(0))
            self.played.pop(0)
        if self.since < self.EVERY or self.count < self.WINDOW:
            return
        self.since = 0
        played = np.concatenate(self.played)
        if np.sqrt(np.mean(played ** 2)) < self.LOUD:
            return
        offset, clarity = lag(np.concatenate(self.heard), played)
        if clarity < self.CLEAR:
            self.candidate = None
            return
        agreed = self.candidate is not None and abs(offset - self.candidate) <= self.AGREE
        self.candidate = offset
        if agreed and abs(offset) * RATE >= 2:
            self.candidate = None
            self.reference.delay = min(1.0, max(0.0, self.reference.delay + offset))
            log.debug("echo delay -> %.0f ms (%+.0f, clarity %.0f)", self.reference.delay * 1000,
                      offset * 1000, clarity)
            self.heard.clear(), self.played.clear()       # the old window is misaligned now
            self.count = 0
