"""Garage mode's voice gate (K1): only Wouter's voice reaches the model, without a wake word.

The live session calls `feed()` with every 100 ms mic chunk and whether it sounds like
speech (the session's own SpeechDetector). The gate holds each new utterance back until
the home server has checked whose voice it is (`check(pcm)` -> True / False / None,
run in a worker thread; ~50 ms on the LAN after `verify_s` of speech):

  idle     keeps a short preroll, waits for speech
  listen   buffers the utterance; after verify_s of speech (or at its end, if it was
           shorter) the check starts; the buffer keeps growing while it runs
  open     it was Wouter: the whole buffer goes up at once, then every chunk live until
           the turn ends (+ a short tail of room sound, so Gemini's own detector closes
           the turn) and an END marker (audioStreamEnd)
  reject   someone else, the radio, his own echo: dropped until the turn ends

When the check cannot run (server down, no profile enrolled) the gate falls back to the
wake word: "Hey Willie" opens a window of `followup_s`, like a normal conversation. The
runner probes the server every so often and calls `set_available(True)` when it is back.
Pure logic plus one thread pool: tests drive it with a fake clock and a fake check.
"""
from __future__ import annotations

import concurrent.futures
from collections import deque

CHUNK_S = 0.1
END = b""                    # marker in feed()'s output: the turn is over (audioStreamEnd)


class VoiceGate:
    def __init__(self, check, verify_s: float = 1.0, *, turn_end_s: float = 0.8, min_speech_s: float = 0.4,
                 preroll_s: float = 0.3, tail_s: float = 0.6, max_hold_s: float = 10.0,
                 followup_s: float = 20.0, spotter_factory=None, on_event=None):
        self.check_fn = check
        self.verify_s, self.turn_end_s, self.min_speech_s = verify_s, turn_end_s, min_speech_s
        self.tail_s, self.max_hold_s, self.followup_s = tail_s, max_hold_s, followup_s
        self.spotter_factory = spotter_factory
        self.on_event = on_event or (lambda kind, detail="": None)
        self.state = "idle"
        self.available = True
        self.preroll: deque[bytes] = deque(maxlen=max(1, round(preroll_s / CHUNK_S)))
        self.recent: deque[bytes] = deque(maxlen=15)       # 1.5 s: "Hey Willie" itself goes up too
        self.buffer: list[bytes] = []
        self.speech_s = 0.0
        self.last_speech = 0.0
        self.quiet_since: float | None = None
        self.window_until = 0.0
        self.pending: concurrent.futures.Future | None = None
        self.spotter = None
        self.stats = {"passed": 0, "rejected": 0, "unavailable": 0}
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="voice-gate")

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def set_available(self, available: bool) -> None:
        if available != self.available:
            self.available = available
            self.on_event("gate", "voice check back" if available else "voice check unavailable: wake word")
            if not available:
                self.state, self.buffer = "idle", []

    # ------------------------------------------------------------ main entry
    def feed(self, chunk: bytes, speech: bool, now: float) -> list[bytes]:
        """One mic chunk in; the chunks to send to the model now out (END = turn over)."""
        if not self.available:
            return self._feed_wake(chunk, speech, now)
        if speech:
            self.last_speech = now
        quiet_for = now - self.last_speech
        if self.state == "idle":
            if not speech:
                self.preroll.append(chunk)
                return []
            self.state, self.buffer, self.speech_s = "listen", [*self.preroll, chunk], CHUNK_S
            self.preroll.clear()
            return []
        if self.state == "listen":
            self.buffer.append(chunk)
            if speech:
                self.speech_s += CHUNK_S
            if self.pending is None:
                ended = quiet_for >= self.turn_end_s
                if self.speech_s >= self.verify_s or (ended and self.speech_s >= self.min_speech_s):
                    self.pending = self._pool.submit(self.check_fn, b"".join(self.buffer))
                elif ended:                              # a click, a cough: no turn
                    self._reset()
                    return []
            if self.pending is not None and self.pending.done():
                return self._decide(now)
            if len(self.buffer) * CHUNK_S > self.max_hold_s:   # the check hangs: give up on it
                self.pending.cancel()
                return self._unavailable("voice check timed out")
            return []
        if self.state == "open":
            out = [chunk]
            if quiet_for >= self.turn_end_s + self.tail_s:
                out.append(END)
                self._reset()
            return out
        # reject: drop until the turn is over
        if quiet_for >= self.turn_end_s:
            self._reset()
        return []

    # ------------------------------------------------------------ internals
    def _decide(self, now: float) -> list[bytes]:
        try:
            verdict = self.pending.result()
        except Exception as exc:                          # the check itself failed
            verdict = None
            self.on_event("gate", f"check failed: {type(exc).__name__}: {exc}")
        self.pending = None
        if verdict is None:
            return self._unavailable("voice check unavailable")
        if verdict:
            self.stats["passed"] += 1
            self.state = "open"
            held, self.buffer = self.buffer, []
            if now - self.last_speech >= self.turn_end_s:  # the utterance already ended
                held.append(END)
                self._reset()
            return held
        self.stats["rejected"] += 1
        self.state, self.buffer = "reject", []
        if now - self.last_speech >= self.turn_end_s:
            self._reset()
        return []

    def _unavailable(self, why: str) -> list[bytes]:
        self.stats["unavailable"] += 1
        held = self.buffer
        self._reset()
        self.set_available(False)
        self.on_event("gate", why)
        # This utterance is lost unless it contained the wake word; feed it to the spotter.
        out: list[bytes] = []
        for chunk in held:
            out += self._feed_wake(chunk, False, self.last_speech)
        return out

    def _reset(self) -> None:
        self.state, self.buffer, self.speech_s = "idle", [], 0.0
        self.pending = None

    def _feed_wake(self, chunk: bytes, speech: bool, now: float) -> list[bytes]:
        """Fallback: the wake word opens a follow-up window, like a normal conversation."""
        self.recent.append(chunk)
        if now < self.window_until:
            if speech:
                self.window_until = max(self.window_until, now + self.followup_s)
            return [chunk]
        if self.spotter_factory is None:
            return []
        if self.spotter is None:
            self.spotter = self.spotter_factory()
        if self.spotter.feed(chunk):
            self.window_until = now + self.followup_s
            self.on_event("wake_again", "")
            held = list(self.recent)
            self.recent.clear()
            return held
        return []
