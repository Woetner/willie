"""480x320 mouth-less face. Standard library only; renders into a RAM framebuffer."""
from __future__ import annotations

import math
import time
import weakref
from dataclasses import dataclass

from . import font
from .framebuffer import Recorder, changed_bands

STATES = ("idle", "curious", "listening", "thinking", "talking", "happy", "sad",
          "surprised", "sleep", "low_battery", "error", "seeing", "show", "connecting", "watched",
          "dancing")
# Where a picture goes, in logical 480x320 coordinates: the whole screen except a 36 px bar
# at the bottom for the title and the mic/camera flags, which stay visible (D17).
PICTURE_BOX = (0, 0, 480, 284)
REVEAL_S = .45              # the photo opens like an eyelid from its centre line
FLASH_S = .12               # camera photos start with a short shutter flash
CYAN, AMBER, RED, WHITE = (57, 208, 255), (255, 190, 72), (255, 99, 105), (221, 238, 242)
# Someone is looking through his camera from the phone app (S8). Its own colour, used for
WATCHED = (255, 60, 90)    # nothing else, so it can never be mistaken for a mood (D17).


@dataclass
class View:
    state: str = "idle"
    text: str = ""
    code: str = ""
    battery: int | None = None
    charging: bool = False
    connected: bool | None = None
    mic: bool = False
    camera: bool = False
    muted: bool = False
    watched: bool = False       # live view open on the phone (S8): red frame on every state
    level: float = 0.0
    gaze: float | None = None
    pet: float = 0.0
    page: int = 0
    image: dict | None = None   # {framebuffer layout: (width, height, pixels)} from Face.show_image
    label: str = ""             # replaces the state's caption, e.g. "RESEARCHING..." (Face.busy)
    image_age: float = 99.0     # seconds since the picture appeared (drives the reveal)
    flash: bool = False         # the picture is a camera photo: shutter flash first
    music: str = ""             # Spotify on his speaker: "TITLE - ARTIST" on the bottom line


def colour(value, fallback):
    try:
        if isinstance(value, str) and len(value) == 7 and value[0] == "#":
            return tuple(int(value[i:i+2], 16) for i in (1, 3, 5))
    except ValueError:
        pass
    return fallback


def mix(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


class Painter:
    """Draw at logical 480x320 coordinates on the actual framebuffer geometry."""
    def __init__(self, surface, brightness, text_cache=None):
        self.fb = surface
        self.sx, self.sy = surface.width / 480, surface.height / 320
        self.brightness = brightness
        self.text_cache = {} if text_cache is None else text_cache

    def ink(self, c):
        return tuple(round(v * self.brightness) for v in c)

    def rect(self, x, y, w, h, c):
        self.fb.rect(round(x*self.sx), round(y*self.sy), max(1, round(w*self.sx)),
                     max(1, round(h*self.sy)), self.ink(c))

    def ellipse(self, x, y, rx, ry, c):
        self.fb.ellipse(round(x*self.sx), round(y*self.sy), max(1, round(rx*self.sx)),
                        max(1, round(ry*self.sy)), self.ink(c))

    def round_rect(self, x, y, w, h, r, c):
        r = max(1, min(r, w/2, h/2))
        x0, y0 = round(x*self.sx), round(y*self.sy)
        x1, y1 = max(x0+1, round((x+w)*self.sx)), max(y0+1, round((y+h)*self.sy))
        self.fb.round_rect(x0, y0, x1, y1, max(1, round(r*min(self.sx, self.sy))), self.ink(c))

    def line(self, x1, y1, x2, y2, width, c):
        count = max(1, int(max(abs(x2-x1), abs(y2-y1))))
        for i in range(count+1):
            t = i/count
            self.ellipse(x1+(x2-x1)*t, y1+(y2-y1)*t, width/2, width/2, c)

    def text(self, text, x, y, scale, c):
        # FONT calls rect(), keeping the same coordinate transform as the eyes. A string
        # is hundreds of small rects, so its ops are built once and reused as one group.
        x, y = round(x), round(y)
        key = (str(text), x, y, scale, self.ink(c))
        op = self.text_cache.get(key)
        if op is None:
            fb, self.fb = self.fb, Recorder(self.fb)
            try:
                font.draw(self, text, x, y, scale, c)
                ops = tuple(self.fb.ops)
            finally:
                fb, self.fb = self.fb, fb
            op = ("g", min(o[1] for o in ops), max(o[2] for o in ops), ops) if ops else ()
            if len(self.text_cache) > 400:
                self.text_cache.clear()
            self.text_cache[key] = op
        if op:
            self.fb.ops.append(op)

    def centre(self, text, y, scale, c):
        self.text(text, (480-len(font.normalise(text))*6*scale)/2, y, scale, c)


class Renderer:
    def __init__(self):
        self.last_time = None
        self.pose = [100.0, 110.0, 100.0, 110.0, 0.0, 0.0]
        self.eye_colour = CYAN
        self.bands: list[tuple[int, int]] = []   # rows repainted by the last draw()
        self._ops = None
        self._surface = lambda: None
        self._text_cache: dict = {}

    def draw(self, surface, view: View, now: float, settings: dict | None = None) -> int:
        """Compose the frame as draw calls, then repaint only the rows whose calls changed
        since the last frame on this surface. Returns the number of rows repainted (0 =
        the frame is identical, nothing to send to the panel); `self.bands` has them."""
        recorder = Recorder(surface)
        self._compose(recorder, view, now, settings or {})
        ops = recorder.ops
        previous = self._ops if self._surface() is surface else None
        self.bands = changed_bands(previous, ops, surface.height)
        if self.bands:
            surface.paint(ops, self.bands)
        self._ops, self._surface = ops, weakref.ref(surface)
        return sum(hi - lo for lo, hi in self.bands)

    def _compose(self, surface, view: View, now: float, settings: dict):
        brightness = max(.05, min(1, float(settings.get("brightness", 80))/100))
        p = Painter(surface, brightness, self._text_cache)
        bg = colour(settings.get("bg_color"), (0, 0, 0))
        base = colour(settings.get("eye_color"), CYAN)
        dim = mix(bg, base, .28)
        surface.fill(p.ink(bg))
        state = view.state
        if state not in STATES:
            state = "idle"
        if state == "show" and hasattr(surface, "layout") and (view.image or {}).get(surface.layout()):
            self._photo(p, view, base, dim, bg)
            return
        # Fixed layout: the animated eye band stays close to B5's measured 138 rows.
        targets = {
            "idle": (100, 106, 100, 106, 0, 0),
            "curious": (100, 118, 89, 91, 7, -3),
            "listening": (101, 104, 101, 104, 0, 0),
            "thinking": (94, 85, 94, 85, -10, -12),
            "talking": (100, 103, 100, 103, 0, 0),
            "happy": (99, 28, 99, 28, 0, 0),
            "sad": (94, 74, 94, 74, 0, 12),
            "surprised": (101, 125, 101, 125, 0, -2),
            "sleep": (93, 7, 93, 7, 0, 17),
            "low_battery": (96, 53, 96, 53, 0, 16),
            "error": (89, 85, 89, 85, 0, 0),
            "seeing": (100, 110, 100, 110, 0, -3),
            "show": (100, 106, 100, 106, 0, 0),
            "connecting": (96, 90, 96, 90, 0, 0),
            "watched": (104, 104, 104, 104, 0, 0),
            "dancing": (99, 28, 99, 28, 0, 0),
        }
        dt = .04 if self.last_time is None else max(0, min(.1, now-self.last_time))
        self.last_time = now
        ease = 1-math.exp(-dt*13)
        target = targets[state]
        self.pose = [v+(t-v)*ease for v,t in zip(self.pose, target)]
        target_colour = RED if state == "error" else WATCHED if state == "watched" else AMBER if state in ("thinking", "low_battery", "connecting") else base
        self.eye_colour = mix(self.eye_colour, target_colour, ease)
        eye = self.eye_colour
        if state == "sleep":
            eye = mix(bg, eye, .35)
        # Asleep (22 Sep, Wouter): only the sleeping eyes, no status bar, flags or captions.
        # The privacy flags come back the moment the mic or camera is actually on (D17).
        bare = state == "sleep" and not (view.mic or view.camera or view.watched)
        if not bare:
            self._status_bar(p, view, base, dim, bg, now)

        if state == "show":
            self._card(p, view, base, dim, bg)
            return
        w1,h1,w2,h2,gx,gy = self.pose
        if state in ("idle", "curious"):
            gx += math.sin(now*.48)*9 + math.sin(now*.17)*5
            gy += math.sin(now*.9)*2
        if state == "seeing":
            # Watching: eyes sweep across the scene like a slow scan.
            gx += math.sin(now*1.6)*24
            gy += math.sin(now*.8)*4
        if view.gaze is not None:
            gx = max(-1, min(1, view.gaze))*18
        if state == "dancing":
            # Music (Spotify): sway on the bar, bob on the beat, at a fixed 120 BPM - the
            # Web API gives no beat any more, and a steady groove reads as dancing anyway.
            gx += math.sin(now*math.pi)*26
            gy -= abs(math.sin(now*math.pi*2))*12
        if state == "talking":
            bounce = max(0, min(1, view.level))
            gy -= bounce*9
            h1 += bounce*17
            h2 += bounce*12
        period = 60/max(.5, float(settings.get("blink_rate", 4)))
        phase = (now+period*.27) % period
        blink = max(.035, abs(phase-.13)/.13) if phase < .26 else 1
        if state in ("sleep", "happy", "error", "dancing"):
            blink = 1
        h1, h2 = h1*blink, h2*blink
        if state == "listening":
            # Pulse in 8 steps: a smooth pulse repainted two 130-row rings every frame (D6).
            ring = mix(bg, base, .22+.12*round(4*(1+math.sin(now*3)))/4)
            for cx in (158, 322):
                p.ellipse(cx, 157, 65, 64, ring)
                p.ellipse(cx, 157, 62, 61, bg)
        style = settings.get("eye_style", "round")
        for side, (cx, w, h) in enumerate(((158+gx,w1,h1),(322+gx,w2,h2))):
            cy = 157+gy
            if state == "watched":
                # Two camera lenses looking back at the viewer: ring, iris, a glint.
                focus = 1+.06*math.sin(now*2.2+side)
                p.ellipse(cx, cy, 52, 52, eye)
                p.ellipse(cx, cy, 43, 43, bg)
                p.ellipse(cx, cy, 30*focus, 30*focus, mix(bg, eye, .55))
                p.ellipse(cx, cy, 15*focus, 15*focus, bg)
                p.ellipse(cx-13, cy-14, 6, 6, WHITE)
            elif state == "error":
                p.line(cx-24, cy-29, cx+24, cy+29, 12, eye)
                p.line(cx-24, cy+29, cx+24, cy-29, 12, eye)
            elif state == "happy":
                # An unmistakable ^ ^ smile, with a small asymmetry.
                p.line(cx-39, cy+12, cx, cy-18-side*3, 12, eye)
                p.line(cx, cy-18-side*3, cx+39, cy+12, 12, eye)
            elif state == "dancing":
                # The same ^ ^, the two eyes rocking against each other like a head tilt.
                tilt = math.sin(now*math.pi)*9*(1 if side else -1)
                p.line(cx-39, cy+12+tilt, cx, cy-18-tilt, 12, eye)
                p.line(cx, cy-18-tilt, cx+39, cy+12-tilt, 12, eye)
            else:
                if style == "visor":
                    h *= .72
                    w *= 1.2
                radius = 3 if style == "pixel" else min(32, h*.42)
                p.round_rect(cx-w/2-2, cy-h/2-2, w+4, h+4, radius+2, mix(bg, eye, .24))
                p.round_rect(cx-w/2, cy-h/2, w, max(3,h), radius, eye)
                if h > 36:
                    p.round_rect(cx-w/2+12, cy-h/2+10, w*.33, 5, 2, mix(eye, WHITE, .45))
                if state == "sad":
                    direction = -1 if side == 0 else 1
                    p.line(cx-w/2-8, cy-h/2-direction*10, cx+w/2+8, cy-h/2+direction*10, 23, bg)
        if view.pet > 0:
            for x, y, size in ((82,115,5),(390,98,7),(405,207,4)):
                y -= (1-view.pet)*14
                p.line(x-size, y, x+size, y, 2, AMBER)
                p.line(x, y-size, x, y+size, 2, AMBER)
        if bare:
            return
        if state in ("thinking", "connecting"):
            for i in range(3):
                p.ellipse(221+i*19, 230, 3, 3, eye if int(now*3)%3 == i else dim)
        if state == "dancing":
            # Two notes drifting up beside the eyes, inside the eye band (D6: few rows).
            for x, offset in ((62, 0.0), (418, 0.5)):
                rise = ((now*.5+offset) % 1)
                self._note(p, x+math.sin(now*3+offset*6)*4, 205-rise*95, mix(bg, eye, 1-rise*.7))
        if state == "sleep":
            p.text("Z", 354, 111+math.sin(now)*3, 2, dim)
            p.text("Z", 378, 95+math.sin(now+1)*3, 1, dim)
        if state == "seeing":
            # Viewfinder corners that breathe in and out, brighter on the way in.
            pulse = .5+.5*math.sin(now*4)
            inset, arm = 6*pulse, 12+6*pulse
            corner = mix(dim, eye, .35+.5*pulse)
            for x, dx in ((77,1),(403,-1)):
                for y, dy in ((92,1),(221,-1)):
                    cx, cy = x+dx*inset, y+dy*inset
                    p.line(cx,cy,cx+dx*arm,cy,3,corner)
                    p.line(cx,cy,cx,cy+dy*arm,3,corner)
        labels = {"idle":"RIGHT HERE", "curious":"OH?", "listening":"I'M LISTENING",
                  "thinking":"LET ME THINK", "talking":"SPEAKING", "happy":"THAT'S NICE",
                  "sad":"OH, WELL", "surprised":"WAIT, WHAT?", "sleep":"RECHARGING" if view.charging else "ZZZ...",
                  "low_battery":"TIME TO RECHARGE", "error":"OOPS", "seeing":"TAKING A LOOK",
                  "connecting":"CONNECTING", "watched":"WOUTER IS WATCHING", "dancing":"GROOVING"}
        # Asleep from the phone app = muted + sleep face: the Zzz label, the MIC MUTED flag stays on top.
        label = "MIC MUTED" if view.muted and state != "sleep" else view.label or labels.get(state, "RIGHT HERE")
        p.centre(label, 271, 2, eye if state in ("error","low_battery","watched") else WHITE)
        detail = view.code[:48] if state == "error" else view.text[:48]
        if view.music and state != "error":
            # Now playing: small, on the bottom line, a note in front of it.
            music = view.music[:44]
            p.centre(music, 299, 1, mix(dim, eye, .5))
            self._note(p, 240-len(music)*3-12, 304, mix(dim, eye, .5), small=True)
        elif detail:
            p.centre(detail, 299, 1, dim)
        else:
            p.line(225, 302, 255, 302, 2, dim)

    def _photo(self, p, view, eye, dim, bg):
        """A picture over (nearly) the whole screen, revealed like an opening eyelid."""
        fb = p.fb
        w, h, pixels = view.image[fb.layout()]
        bx, by, bw, bh = PICTURE_BOX
        x = round((bx + (bw - w / p.sx) / 2) * p.sx)
        y = round((by + (bh - h / p.sy) / 2) * p.sy)
        fb.blit(x, y, w, h, pixels)
        age = max(0.0, view.image_age)
        if view.flash and age < FLASH_S:
            fb.rect(x, y, w, h, p.ink(mix(WHITE, (255, 255, 255), .5)))
        elif age < REVEAL_S + (FLASH_S if view.flash else 0):
            t = (age - (FLASH_S if view.flash else 0)) / REVEAL_S
            t = 1 - (1 - max(0.0, min(1.0, t))) ** 3              # ease-out
            half = round(h / 2 * t)
            mid = y + h // 2
            fb.rect(x, y, w, mid - half - y, p.ink(bg))             # upper lid
            fb.rect(x, mid + half, w, y + h - mid - half, p.ink(bg))  # lower lid
            for edge in (mid - half - 3, mid + half):
                fb.rect(x, edge, w, 3, p.ink(eye))
        # Bottom bar: title left, the privacy flags right (they must stay visible, D17).
        p.rect(0, 286, 480, 1, dim)
        p.text(font.normalise(view.text)[:28], 14, 296, 2, WHITE)
        if view.watched:
            self._watched_frame(p, time.monotonic())
        if view.camera:
            p.ellipse(372, 302, 3, 3, RED)
            p.text("CAM", 380, 298, 1, WHITE)
        mic = "MUTED" if view.muted else "MIC" if view.mic else "MIC OFF"
        p.text(mic, 414, 298, 1, AMBER if view.muted else eye if view.mic else dim)

    @staticmethod
    def _note(p, x, y, c, small=False):
        """A quaver: head, stem, flag. (x, y) is the centre of the head."""
        k = .45 if small else 1
        p.ellipse(x, y, 7*k, 5*k, c)
        p.line(x+6*k, y, x+6*k, y-22*k, max(1, 3*k), c)
        p.line(x+6*k, y-22*k, x+14*k, y-14*k, max(1, 3*k), c)

    def _status_bar(self, p, view, base, dim, bg, now):
        p.rect(23, 23, 5, 13, AMBER)
        p.text("WILL-E", 37, 23, 2, WHITE)
        # Unknown is explicitly unknown, never a fabricated battery or connection.
        link = "LINK --" if view.connected is None else "LINK OK" if view.connected else "OFFLINE"
        p.text(link, 229, 25, 1, dim if view.connected is None else base if view.connected else AMBER)
        battery = "--" if view.battery is None else f"{view.battery}%"
        p.text(battery, 357, 23, 2, AMBER if view.battery is not None and view.battery < 20 else WHITE)
        p.round_rect(418, 21, 34, 17, 3, dim)
        p.rect(421, 24, 28, 11, bg)
        p.rect(453, 26, 3, 7, dim)
        if view.battery is not None:
            fill = max(0, min(100, view.battery))*24/100
            if fill:
                p.rect(423, 26, fill, 7, AMBER if view.battery < 20 else base)
        if view.charging:
            p.text("+", 407, 23, 1, AMBER)
        # These flags report actual device activity, independently of expression.
        privacy = "MIC MUTED" if view.muted else "MIC LIVE" if view.mic else "MIC OFF"
        p.text(privacy, 24, 53, 2, AMBER if view.muted else base if view.mic else dim)
        if view.watched:
            self._watched_frame(p, now)
        elif view.camera:
            p.ellipse(336, 59, 3, 3, RED)
            p.text("CAM LIVE", 349, 53, 2, WHITE)

    def _watched_frame(self, p, now):
        """Live view is open (S8): a red frame round the whole screen and a blinking LIVE
        badge, over every expression, so a viewer can never be missed (D17)."""
        pulse = .6+.4*math.sin(now*3)
        edge = mix((0, 0, 0), WATCHED, pulse)
        for x, y, w, h in ((0, 0, 480, 5), (0, 315, 480, 5), (0, 0, 5, 320), (475, 0, 5, 320)):
            p.rect(x, y, w, h, edge)
        p.round_rect(334, 49, 124, 22, 4, WATCHED)
        if int(now*2) % 2 == 0:
            p.ellipse(346, 60, 4, 4, WHITE)
        p.text("LIVE VIEW", 355, 54, 1, WHITE)

    def _card(self, p, view, eye, dim, bg):
        picture = (view.image or {}).get(p.fb.layout()) if hasattr(p.fb, "layout") else None
        if picture:
            # Photo card: as big as the status bar allows, title and hint underneath.
            w, h, pixels = picture
            bx, by, bw, bh = PICTURE_BOX
            x = round((bx + (bw - w / p.sx) / 2) * p.sx)
            y = round((by + (bh - h / p.sy) / 2) * p.sy)
            p.fb.blit(x, y, w, h, pixels)
            p.text(font.normalise(view.text)[:40], 24, 288, 2, WHITE)
            p.text("TAP TO RETURN", 368, 293, 1, dim)
            return
        for cx in (423, 446):
            p.round_rect(cx-7, 77, 14, 20, 5, eye)
        p.text("ON MY MIND", 24, 81, 1, dim)
        text = view.text or "..."
        if len(font.normalise(text)) <= 12 and "\n" not in text:
            scale = min(7, max(3, 420//(max(1,len(font.normalise(text)))*6)))
            p.centre(text, 155, scale, WHITE)
            pages, page = 1, 0
        else:
            wrapped = font.lines(text, 36)
            pages = max(1, (len(wrapped)+6)//7)
            page = view.page % pages
            for i, line in enumerate(wrapped[page*7:(page+1)*7]):
                p.text(line, 24, 111+i*22, 2, WHITE)
        p.text("TAP TO TURN PAGE" if pages > 1 else "TAP TO RETURN", 24, 285, 1, dim)
        if pages > 1:
            p.text(f"{page+1}/{pages}", 414, 285, 1, eye)
