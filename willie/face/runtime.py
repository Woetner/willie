"""One face owner: animation, voice events, touch and config; no extra service."""
from __future__ import annotations

import array
import logging
import math
import os
import struct
import sys
import threading
import time
from collections import deque
from dataclasses import replace
from pathlib import Path

from . import font
from .framebuffer import Framebuffer, open_all
from .renderer import STATES, View, Renderer

log = logging.getLogger("willie.face")


class Touch:
    """Read BTN_TOUCH releases; petting the whole panel needs no x/y calibration."""
    EVENT = struct.Struct("llHHi")

    def __init__(self, path):
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self.pending = b""
        self.down = False

    @classmethod
    def discover(cls):
        try:
            blocks = Path("/proc/bus/input/devices").read_text().split("\n\n")
            for block in blocks:
                if "ads7846" in block.lower() or "xpt2046" in block.lower():
                    for token in block.split():
                        if token.startswith("event") and token[5:].isdigit():
                            return cls("/dev/input/" + token)
        except OSError as exc:
            log.info("touch unavailable: %s", exc)
        return None

    def poll(self):
        try:
            self.pending += os.read(self.fd, self.EVENT.size*64)
        except BlockingIOError:
            return False
        tapped = False
        n = len(self.pending)//self.EVENT.size*self.EVENT.size
        for offset in range(0, n, self.EVENT.size):
            _, _, kind, code, value = self.EVENT.unpack_from(self.pending, offset)
            if kind == 0 and code == 3:  # SYN_DROPPED: discard any incomplete gesture
                self.down = False
            if kind == 1 and code == 330:
                tapped |= self.down and value == 0
                self.down = bool(value)
        self.pending = self.pending[n:]
        return tapped

    def close(self):
        os.close(self.fd)


class Face:
    def __init__(self, displays=(), *, config=None, touch=None, clock=time.monotonic, autostart=True):
        self.displays = list(displays)
        self.config, self.touch, self.clock = config, touch, clock
        self.settings = config.snapshot()["face"] if config else {}
        self._view = View()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._closed = False
        self._pet_until = self._show_until = self._show_started = 0.0
        self._shown_text = ""
        self._page_offset = 0
        self._audio = deque(maxlen=1500)  # 30 s at 20 ms; amplitudes only, no stored audio
        self._audio_until = 0.0
        self._return_to_listening = False
        self._level = 0.0
        self.failure = None
        self.frames = self.dirty_rows = 0
        self.render_seconds = self.max_render_seconds = 0.0
        if autostart and self.displays:
            self.start()

    @classmethod
    def open(cls):
        """Use the SPI panel alone when present, otherwise an available framebuffer."""
        displays = []
        try:
            for name in sorted(Path("/sys/class/graphics").glob("fb*/name")):
                if "ili9486" in name.read_text().lower():
                    displays = [Framebuffer.open("/dev/" + name.parent.name)]
                    break
            if not displays:
                displays = open_all()
            from willie.config import Config
            return cls(displays, config=Config(), touch=Touch.discover())
        except Exception:
            for display in displays:
                display.close()
            raise

    @classmethod
    def optional(cls):
        try:
            return cls.open()
        except (OSError, RuntimeError) as exc:
            log.info("face unavailable: %s", exc)
            return None

    def start(self):
        if self._closed:
            raise RuntimeError("face is closed")
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="willie-face", daemon=True)
            self._thread.start()

    def _run(self):
        buffers = [d.back_buffer() for d in self.displays]
        renderers = [Renderer() for _ in self.displays]
        next_config = 0.0
        try:
            while not self._stop.is_set():
                now = self.clock()
                if self.config and now >= next_config:
                    try:
                        self.config.reload()
                        self.settings = self.config.snapshot()["face"]
                    except Exception as exc:
                        log.warning("face settings unchanged: %s", exc)
                    next_config = now+2
                if self.touch:
                    try:
                        if self.touch.poll():
                            self.pet()
                    except OSError as exc:
                        log.warning("touch disconnected: %s", exc)
                        self.touch.close()
                        self.touch = None
                render_started = self.clock()
                view = self.snapshot(now)
                for renderer, buffer, display in zip(renderers, buffers, self.displays):
                    renderer.draw(buffer, view, now, self.settings)
                    self.dirty_rows += display.present(buffer)
                elapsed = self.clock()-render_started
                self.render_seconds += elapsed
                self.max_render_seconds = max(self.max_render_seconds, elapsed)
                self.frames += 1
                fps = max(5, min(30, float(self.settings.get("fps", 25))))
                self._stop.wait(max(0, 1/fps-(self.clock()-now)))
        except Exception as exc:
            self.failure = exc
            log.exception("face animation stopped")
            self._stop.set()

    def set_state(self, state, text="", *, code=""):
        state = state.replace(" ", "_")
        if state not in STATES:
            raise ValueError(f"unknown face state: {state}")
        if state == "show":
            self.show(text or "...")
            return
        with self._lock:
            self._view.state, self._view.text, self._view.code = state, str(text), str(code)
            if state == "error":
                self._show_until = 0

    def indicators(self, **values):
        """Report real device state. This API does not operate or mute a microphone."""
        allowed = {"battery", "charging", "connected", "mic", "camera", "muted", "gaze"}
        if set(values)-allowed:
            raise ValueError("unknown face indicator")
        if values.get("battery") is not None and "battery" in values:
            values["battery"] = max(0, min(100, int(values["battery"])))
        with self._lock:
            for key, value in values.items():
                setattr(self._view, key, value)

    def show(self, text):
        text = str(text).strip()[:4096]
        if not text:
            return {"fout": "geen tekst"}
        with self._lock:
            self._shown_text = text
            self._show_started = self.clock()
            pages = max(1, (len(font.lines(text, 36))+6)//7)
            self._show_until = self._show_started+max(15, pages*6)
            self._page_offset = 0
        return {"getoond": text}

    def dismiss(self):
        with self._lock:
            self._show_until = 0

    def pet(self):
        now = self.clock()
        with self._lock:
            if now < self._pet_until-1.15:  # 250 ms contact debounce
                return
            if now < self._show_until:
                pages = max(1, (len(font.lines(self._shown_text, 36))+6)//7)
                if pages == 1:
                    self._show_until = 0
                else:
                    self._page_offset += 1
                    self._show_started = now
                    self._show_until = now+max(15,pages*6)
            self._pet_until = now+1.4

    def audio(self, pcm: bytes, rate=24000, *, starts_at=None):
        """Queue 20 ms RMS envelopes at playback time, rather than at network arrival."""
        if rate <= 0:
            raise ValueError("sample rate must be positive")
        samples = array.array("h")
        samples.frombytes(pcm[:len(pcm)//2*2])
        if sys.byteorder != "little":
            samples.byteswap()
        now = self.clock()
        with self._lock:
            start = max(now, self._audio_until) if starts_at is None else starts_at
            step = max(1, rate//50)
            for offset in range(0, len(samples), step):
                window = samples[offset:offset+step]
                rms = math.sqrt(sum(v*v for v in window)/len(window))/32768
                self._audio.append((start+offset/rate, min(1, rms*5)))
            self._audio_until = start+len(samples)/rate
            self._view.state = "talking"

    def event(self, kind, detail=""):
        with self._lock:
            if kind == "ready":
                self._view.connected = True
                self.set_state("listening")
            elif kind == "mic_active":
                self._view.mic = True
            elif kind == "mic_idle":
                self._view.mic = False
            elif kind == "user_speaking":
                if self._view.state != "talking":
                    self.set_state("listening")
            elif kind == "user_turn_end":
                if self._view.state != "talking":
                    self.set_state("thinking")
            elif kind == "speaking":
                self._return_to_listening = False
                self.set_state("talking")
            elif kind == "interrupted":
                self._audio.clear()
                self._level = self._audio_until = 0
                self._return_to_listening = False
                self.set_state("listening")
            elif kind == "turn_complete":
                self._return_to_listening = True
            elif kind == "error":
                self._audio.clear()
                self._return_to_listening = False
                self._level = self._audio_until = 0
                self.set_state("error", code="VOICE ERROR")
            elif kind == "closed":
                self._view.connected = False
                self._view.mic = self._view.camera = False
                self._audio.clear()
                self._level = self._audio_until = 0
                self._return_to_listening = False
                self._show_until = 0
                self.set_state("idle")

    def snapshot(self, now=None):
        now = self.clock() if now is None else now
        with self._lock:
            while self._audio and self._audio[0][0] <= now:
                _, self._level = self._audio.popleft()
            if now >= self._audio_until:
                self._level = 0.0
                if self._return_to_listening:
                    self.set_state("listening")
                    self._return_to_listening = False
            v = replace(self._view, level=self._level)
            v.pet = max(0, min(1, (self._pet_until-now)/1.4))
            if (v.battery is not None and v.battery < 20 and not v.charging
                    and v.state in ("idle", "sleep", "curious", "happy", "sad")):
                v.state = "low_battery"
            if v.pet and v.state in ("idle", "happy", "sleep", "curious"):
                v.state = "happy"
            if now < self._show_until and v.state not in ("error", "low_battery"):
                v.state, v.text = "show", self._shown_text
                v.page = int((now-self._show_started)//6)+self._page_offset
            return v

    # Compatibility with the old blocking keyboard / bench voice console.
    def idle(self, question=""):
        self._show_until = 0
        self.set_state("idle", question[-48:])

    def waiting(self):
        self.indicators(mic=False, camera=False)
        self.set_state("idle", "SAY HEY WILLIE")

    def status(self, label, colour=None):
        label = label.upper()
        state = {"SEEING":"seeing", "THINKING":"thinking", "HEARING":"thinking",
                 "LISTENING":"listening", "YES?":"listening"}.get(label, "thinking")
        self.indicators(camera=state == "seeing", mic=state == "listening")
        self.set_state(state)

    def answer(self, answer, talking=False):
        self.indicators(camera=False, mic=False)
        self.set_state("talking" if talking else "idle")
        if not talking:
            self.show(answer)

    def error(self, message):
        self.indicators(camera=False, mic=False)
        self.set_state("error", code="PLEASE TRY AGAIN")

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._thread:
            self._thread.join()
        if self.touch:
            self.touch.close()
        # Leave truthful inactive indicators on the glass when the session exits.
        self.event("closed")
        for display in self.displays:
            try:
                buffer = display.back_buffer()
                Renderer().draw(buffer, self.snapshot(), self.clock(), self.settings)
                display.present(buffer)
            except (OSError, ValueError):
                log.warning("could not paint inactive face on exit")
            finally:
                display.close()
