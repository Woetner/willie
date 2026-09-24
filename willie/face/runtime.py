"""One face owner: animation, voice events, touch and config; no extra service."""
from __future__ import annotations

import array
import atexit
import fcntl
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
from .renderer import PICTURE_BOX, STATES, View, Renderer

log = logging.getLogger("willie.face")


class Touch:
    """Read BTN_TOUCH releases; petting the whole panel needs no x/y calibration.

    Since K1 (pinouts) it also follows the finger: `poll()` still returns True on a release
    (a pet), and leaves the gestures of that poll in `self.gestures`:
      ("tap", x, y)     short touch without moving     ("long", x, y)  held >= LONG_S still
      ("drag", dx, dy)  the finger moved (screen px)   ("release",)    after a drag
    Screen x/y come from the raw ADC through the face.touch_* settings (calibration)."""
    EVENT = struct.Struct("llHHi")
    MOVE_PX = 12            # more than this from the start point is a drag, not a tap
    LONG_S = 1.0
    raw = (None, None)
    start = last = None
    down_at = 0.0
    moved = False
    calibration: dict = {}

    def __init__(self, path):
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self.pending = b""
        self.down = False
        self.gestures: list[tuple] = []

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

    def screen(self, raw_x, raw_y):
        """Raw ADC -> logical 480x320 screen coordinates (face.touch_* settings)."""
        c = self.calibration or {}
        x0, x1 = float(c.get("touch_x_min", 900)), float(c.get("touch_x_max", 3200))
        y0, y1 = float(c.get("touch_y_min", 580)), float(c.get("touch_y_max", 2960))
        if c.get("touch_swap_xy"):
            raw_x, raw_y = raw_y, raw_x
        fx = (raw_x - x0) / ((x1 - x0) or 1)
        fy = (raw_y - y0) / ((y1 - y0) or 1)
        if c.get("touch_flip_x"):
            fx = 1 - fx
        if c.get("touch_flip_y"):
            fy = 1 - fy
        return max(0.0, min(480.0, fx * 480)), max(0.0, min(320.0, fy * 320))

    def poll(self, now=None):
        self.gestures = []
        try:
            self.pending += os.read(self.fd, self.EVENT.size*64)
        except BlockingIOError:
            return False
        now = time.monotonic() if now is None else now
        tapped = False
        n = len(self.pending)//self.EVENT.size*self.EVENT.size
        for offset in range(0, n, self.EVENT.size):
            _, _, kind, code, value = self.EVENT.unpack_from(self.pending, offset)
            if kind == 0 and code == 3:  # SYN_DROPPED: discard any incomplete gesture
                self.down, self.start = False, None
            elif kind == 3 and code in (0, 1):                   # ABS_X / ABS_Y
                self.raw = (value, self.raw[1]) if code == 0 else (self.raw[0], value)
            elif kind == 0 and code == 0 and self.down and None not in self.raw:   # SYN_REPORT
                pos = self.screen(*self.raw)
                if self.start is None:
                    self.start = self.last = pos
                elif self.moved or math.dist(pos, self.start) > self.MOVE_PX:
                    self.moved = True
                    self.gestures.append(("drag", pos[0] - self.last[0], pos[1] - self.last[1]))
                    self.last = pos
            if kind == 1 and code == 330:
                if self.down and value == 0:
                    tapped = True
                    where = self.start or (240.0, 160.0)
                    if self.moved:
                        self.gestures.append(("release",))
                    elif now - self.down_at >= self.LONG_S:
                        self.gestures.append(("long", *where))
                    else:
                        self.gestures.append(("tap", *where))
                if value and not self.down:
                    self.down_at, self.start, self.moved = now, None, False
                self.down = bool(value)
        self.pending = self.pending[n:]
        return tapped

    def close(self):
        os.close(self.fd)


class Console:
    """Keep the Linux text console off the panel while the face owns it.

    `fbcon=map:11` puts tty1 on the ILI9486 (the login screen lives there), so
    without this the console's blinking cursor shows through the left eye and any
    console output would draw over the face. KD_GRAPHICS tells fbcon to stop
    drawing on that VT; KD_TEXT on close gives the login screen back.
    """
    KDSETMODE, KD_TEXT, KD_GRAPHICS = 0x4B3A, 0x00, 0x01

    def __init__(self, path="/dev/tty1"):
        self.fd, self.mode = None, ""
        try:
            self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        except OSError as exc:
            log.info("console not reachable: %s", exc)
            return
        try:
            fcntl.ioctl(self.fd, self.KDSETMODE, self.KD_GRAPHICS)   # needs root (the service)
            self.mode = "graphics"
        except OSError:
            # As the logged-in user we own tty1 but may not switch its mode:
            # hiding the cursor is what we can do, and it removes the blinking square.
            try:
                os.write(self.fd, b"\033[?25l")
                self.mode = "cursor"
            except OSError as exc:
                log.info("console cursor stays: %s", exc)
                os.close(self.fd)
                self.fd = None
                return
        atexit.register(self.restore)              # Ctrl-C / normal exit, not SIGKILL

    def restore(self):
        if self.fd is None:
            return
        try:
            if self.mode == "graphics":
                fcntl.ioctl(self.fd, self.KDSETMODE, self.KD_TEXT)
            else:
                os.write(self.fd, b"\033[?25h")
        except OSError:
            pass
        os.close(self.fd)
        self.fd = None


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
        self._dance_until = 0.0
        self._shown_text = ""
        self._shown_image = None
        self._shown_flash = False
        self._page_offset = 0
        self._pinout = None          # K1: willie.face.pinout.Pinout while a pinout is shown
        self._pinout_until = 0.0
        self._last_tap = None        # (time, x, y) of a tap on the pinout: double-tap = zoom
        self._audio = deque(maxlen=1500)  # 30 s at 20 ms; amplitudes only, no stored audio
        self._audio_until = 0.0
        self._return_to_listening = False
        self._busy: list[str] = []   # labels of running background jobs (Face.busy)
        self._level = 0.0
        self.on_pet = None           # fn() on every accepted touch (mood event, G2)
        self.failure = None
        self.console = None
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
            face = cls(displays, config=Config(), touch=Touch.discover())
            face.console = Console()
            return face
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
                        self.touch.calibration = self.settings
                        released = self.touch.poll()
                        if self._pinout_on(now):
                            for gesture in self.touch.gestures:
                                self.pinout_gesture(gesture, now)
                        elif released:
                            self.pet()
                    except OSError as exc:
                        log.warning("touch disconnected: %s", exc)
                        self.touch.close()
                        self.touch = None
                render_started = self.clock()
                view = self.snapshot(now)
                for renderer, buffer, display in zip(renderers, buffers, self.displays):
                    if renderer.draw(buffer, view, now, self.settings):   # 0 = same frame
                        self.dirty_rows += display.present(buffer, renderer.bands)
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

    def music(self, now_playing: str) -> None:
        """Spotify on his speaker: "TITLE - ARTIST", or "" when no music plays."""
        with self._lock:
            self._view.music = str(now_playing)[:80]

    def activity(self, badges) -> None:
        """Background jobs from the hub as small icons in the status row (see activity_badges)."""
        with self._lock:
            self._view.badges = tuple(tuple(b) for b in badges)[:4]

    def mode(self, name: str) -> None:
        """A Phase K mode is on ("GARAGE", "SENTRY", ...) or "" for none: a label next to his name."""
        with self._lock:
            self._view.mode = str(name)[:12]

    def dance(self, seconds: float) -> None:
        """Dance to the music for a while - only over a resting face (idle, curious, happy)."""
        with self._lock:
            self._dance_until = self.clock()+max(0.0, float(seconds))

    def indicators(self, **values):
        """Report real device state. This API does not operate or mute a microphone."""
        allowed = {"battery", "charging", "connected", "mic", "camera", "muted", "gaze", "watched"}
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
            self._shown_image = None
            self._pinout_until = 0
            self._show_started = self.clock()
            pages = max(1, (len(font.lines(text, 36))+6)//7)
            self._show_until = self._show_started+max(15, pages*6)
            self._page_offset = 0
        return {"getoond": text}

    def show_image(self, picture, seconds=20, flash=False):
        """Put a `willie.face.picture.Picture` on the show card. Scaling and pixel
        conversion happen here, once, in the caller's thread - never in the render loop."""
        _, _, bw, bh = PICTURE_BOX
        prepared = {}
        for display in self.displays:
            key = display.layout()
            if key not in prepared:
                sx, sy = display.width / 480, display.height / 320
                fitted = picture.fit(int(bw * sx), int(bh * sy))
                prepared[key] = (fitted.width, fitted.height, display.convert(fitted.rgb))
        with self._lock:
            self._shown_text = picture.title
            self._shown_image = prepared
            self._shown_flash = flash
            self._pinout_until = 0
            self._show_started = self.clock()
            self._show_until = self._show_started + seconds
            self._page_offset = 0
        return {"getoond": picture.title}

    def dismiss(self):
        with self._lock:
            self._show_until = 0
            self._pinout_until = 0

    # ---- pinouts (K1) -----------------------------------------------------------
    PINOUT_S = 600.0                 # a pinout stays up this long unless closed

    def show_pinout(self, data: dict, pin: str = ""):
        """Put a pinout from the home server (hub/garage.py) on the face; `pin` lights one
        pin up and zooms in on it."""
        from .pinout import Layout, Pinout
        layout = Layout.build(data)
        if not layout.pins:
            return {"fout": "geen pinnen in de pinout"}
        view = Pinout(layout)
        found = view.focus(pin) if pin else None
        with self._lock:
            self._pinout, self._last_tap = view, None
            self._pinout_until = self.clock() + self.PINOUT_S
            self._show_until = 0
        result = {"getoond": layout.title, "pinnen": len(layout.pins)}
        if pin:
            result["pin"] = f"{found.nr} {found.label()}" if found else f"pin {pin} niet gevonden"
        return result

    def pinout_focus(self, pin: str):
        with self._lock:
            if not self._pinout_on(self.clock()):
                return {"fout": "er staat geen pinout op het scherm"}
            found = self._pinout.focus(pin)
            self._pinout_until = self.clock() + self.PINOUT_S
        return {"pin": f"{found.nr} {found.label()}"} if found else {"fout": f"pin {pin} niet gevonden"}

    def _pinout_on(self, now) -> bool:
        return self._pinout is not None and now < self._pinout_until

    def pinout_gesture(self, gesture: tuple, now: float) -> None:
        """Double-tap = next zoom step at that point, drag = pan, long press = close."""
        with self._lock:
            view = self._pinout
            if view is None:
                return
            self._pinout_until = now + self.PINOUT_S
            kind = gesture[0]
            if kind == "drag":
                view.pan(gesture[1], gesture[2])
            elif kind == "long":
                self._pinout_until = 0
            elif kind == "tap":
                _, x, y = gesture
                last = self._last_tap
                if last and now - last[0] < 0.45 and math.dist((x, y), last[1:]) < 60:
                    view.zoom_step(x, min(y, 283))
                    self._last_tap = None
                else:
                    self._last_tap = (now, x, y)

    def pet(self):
        now = self.clock()
        with self._lock:
            if now < self._pet_until-1.15:  # 250 ms contact debounce
                return
            if now < self._show_until:
                pages = 1 if self._shown_image else max(1, (len(font.lines(self._shown_text, 36))+6)//7)
                if pages == 1:
                    self._show_until = 0
                else:
                    self._page_offset += 1
                    self._show_started = now
                    self._show_until = now+max(15,pages*6)
            self._pet_until = now+1.4
        if self.on_pet:
            self.on_pet()

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

    def busy(self, label, state="thinking"):
        """While the returned context is open, the face shows `state` ("thinking", or
        "seeing" for the camera) with `label` whenever it is not talking - so a 15 s web
        search or a photo upload does not look like a hang (or like listening)."""
        face = self

        class _Busy:
            def __enter__(self):
                with face._lock:
                    face._busy.append((label, state))
                return self

            def __exit__(self, *exc):
                with face._lock:
                    face._busy.remove((label, state))
                return False
        return _Busy()

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
            if self._busy and v.state not in ("talking", "error", "low_battery"):
                v.label, v.state = self._busy[-1]
            v.pet = max(0, min(1, (self._pet_until-now)/1.4))
            if (v.battery is not None and v.battery < 20 and not v.charging
                    and v.state in ("idle", "sleep", "curious", "happy", "sad")):
                v.state = "low_battery"
            if now < self._dance_until and v.music and v.state in ("idle", "curious", "happy"):
                v.state = "dancing"
            if v.pet and v.state in ("idle", "happy", "sleep", "curious"):
                v.state = "happy"
            # Live view (S8): the resting expressions give way to the "watched" lenses; talking,
            # listening, errors keep their own face and get the red LIVE frame on top.
            if v.watched and v.state in ("idle", "sleep", "curious", "happy", "sad", "seeing", "dancing"):
                v.state = "watched"
            if self._pinout_on(now) and v.state not in ("error", "low_battery"):
                v.state, v.pinout = "pinout", self._pinout.frozen()
            elif now < self._show_until and v.state not in ("error", "low_battery"):
                v.state, v.text, v.image = "show", self._shown_text, self._shown_image
                v.image_age, v.flash = now - self._show_started, self._shown_flash
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
        if self.console:
            self.console.restore()


# The hub's activity list (homeserver hub/activity.py, MQTT willie/activity) -> face badges.
# Only what he is actively doing for Wouter; the always-on eufy watch would be a permanent
# icon that says nothing, so it stays in the app.
FACE_ICONS = {"printer", "search", "eye", "bell", "timer", "air"}
BELL_WITHIN_S = 3600


def activity_badges(snapshot: dict, now: float | None = None) -> list[tuple[str, str, str]]:
    from datetime import datetime

    now = time.time() if now is None else now
    out = []
    for item in (snapshot or {}).get("background") or []:
        icon, level = item.get("icon"), item.get("level") or "ok"
        if item.get("id") == "eufy" or icon not in FACE_ICONS:
            continue
        text = ""
        if item.get("id") == "printer" and isinstance(item.get("progress"), (int, float)):
            text = f"{round(item['progress']*100)}%"
        elif item.get("at_iso"):
            try:
                left = datetime.fromisoformat(item["at_iso"]).timestamp() - now
            except ValueError:
                continue
            if icon == "bell" and left > BELL_WITHIN_S:
                continue                        # a reminder next week is not "doing something"
            text = f"{max(0, round(left/60))}M"
        out.append((icon, text, level))
    return out[:4]

