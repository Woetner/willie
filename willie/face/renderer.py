"""480x320 mouth-less face. Standard library only; renders into a RAM framebuffer."""
from __future__ import annotations

import math
from dataclasses import dataclass

from . import font

STATES = ("idle", "curious", "listening", "thinking", "talking", "happy", "sad",
          "surprised", "sleep", "low_battery", "error", "seeing", "show", "connecting")
CYAN, AMBER, RED, WHITE = (57, 208, 255), (255, 190, 72), (255, 99, 105), (221, 238, 242)


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
    level: float = 0.0
    gaze: float | None = None
    pet: float = 0.0
    page: int = 0


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
    def __init__(self, surface, brightness):
        self.fb = surface
        self.sx, self.sy = surface.width / 480, surface.height / 320
        self.brightness = brightness

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
        self.rect(x+r, y, w-2*r, h, c)
        self.rect(x, y+r, w, h-2*r, c)
        for cx in (x+r, x+w-r):
            for cy in (y+r, y+h-r):
                self.ellipse(cx, cy, r, r, c)

    def line(self, x1, y1, x2, y2, width, c):
        count = max(1, int(max(abs(x2-x1), abs(y2-y1))))
        for i in range(count+1):
            t = i/count
            self.ellipse(x1+(x2-x1)*t, y1+(y2-y1)*t, width/2, width/2, c)

    def text(self, text, x, y, scale, c):
        # FONT calls rect(), keeping the same coordinate transform as the eyes.
        font.draw(self, text, x, y, scale, c)

    def centre(self, text, y, scale, c):
        self.text(text, (480-len(font.normalise(text))*6*scale)/2, y, scale, c)


class Renderer:
    def __init__(self):
        self.last_time = None
        self.pose = [100.0, 110.0, 100.0, 110.0, 0.0, 0.0]
        self.eye_colour = CYAN

    def draw(self, surface, view: View, now: float, settings: dict | None = None):
        settings = settings or {}
        brightness = max(.05, min(1, float(settings.get("brightness", 80))/100))
        p = Painter(surface, brightness)
        bg = colour(settings.get("bg_color"), (0, 0, 0))
        base = colour(settings.get("eye_color"), CYAN)
        dim = mix(bg, base, .28)
        surface.fill(p.ink(bg))
        state = view.state
        if state not in STATES:
            state = "idle"
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
        }
        dt = .04 if self.last_time is None else max(0, min(.1, now-self.last_time))
        self.last_time = now
        ease = 1-math.exp(-dt*13)
        target = targets[state]
        self.pose = [v+(t-v)*ease for v,t in zip(self.pose, target)]
        target_colour = RED if state == "error" else AMBER if state in ("thinking", "low_battery", "connecting") else base
        self.eye_colour = mix(self.eye_colour, target_colour, ease)
        eye = self.eye_colour
        if state == "sleep":
            eye = mix(bg, eye, .35)
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
        if view.camera:
            p.ellipse(336, 59, 3, 3, RED)
            p.text("CAM LIVE", 349, 53, 2, WHITE)

        if state == "show":
            self._card(p, view, base, dim, bg)
            return
        w1,h1,w2,h2,gx,gy = self.pose
        if state in ("idle", "curious"):
            gx += math.sin(now*.48)*9 + math.sin(now*.17)*5
            gy += math.sin(now*.9)*2
        if view.gaze is not None:
            gx = max(-1, min(1, view.gaze))*18
        if state == "talking":
            bounce = max(0, min(1, view.level))
            gy -= bounce*9
            h1 += bounce*17
            h2 += bounce*12
        period = 60/max(.5, float(settings.get("blink_rate", 4)))
        phase = (now+period*.27) % period
        blink = max(.035, abs(phase-.13)/.13) if phase < .26 else 1
        if state in ("sleep", "happy", "error"):
            blink = 1
        h1, h2 = h1*blink, h2*blink
        if state == "listening":
            ring = mix(bg, base, .22+.12*(1+math.sin(now*3)))
            for cx in (158, 322):
                p.ellipse(cx, 157, 65, 64, ring)
                p.ellipse(cx, 157, 62, 61, bg)
        style = settings.get("eye_style", "round")
        for side, (cx, w, h) in enumerate(((158+gx,w1,h1),(322+gx,w2,h2))):
            cy = 157+gy
            if state == "error":
                p.line(cx-24, cy-29, cx+24, cy+29, 12, eye)
                p.line(cx-24, cy+29, cx+24, cy-29, 12, eye)
            elif state == "happy":
                # An unmistakable ^ ^ smile, with a small asymmetry.
                p.line(cx-39, cy+12, cx, cy-18-side*3, 12, eye)
                p.line(cx, cy-18-side*3, cx+39, cy+12, 12, eye)
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
        if state in ("thinking", "connecting"):
            for i in range(3):
                p.ellipse(221+i*19, 230, 3, 3, eye if int(now*3)%3 == i else dim)
        if state == "sleep":
            p.text("Z", 354, 111+math.sin(now)*3, 2, dim)
            p.text("Z", 378, 95+math.sin(now+1)*3, 1, dim)
        if state == "seeing":
            for x, dx in ((77,1),(403,-1)):
                for y, dy in ((92,1),(221,-1)):
                    p.line(x,y,x+dx*12,y,2,dim)
                    p.line(x,y,x,y+dy*12,2,dim)
        labels = {"idle":"RIGHT HERE", "curious":"OH?", "listening":"I'M LISTENING",
                  "thinking":"LET ME THINK", "talking":"SPEAKING", "happy":"THAT'S NICE",
                  "sad":"OH, WELL", "surprised":"WAIT, WHAT?", "sleep":"RECHARGING" if view.charging else "ZZZ...",
                  "low_battery":"TIME TO RECHARGE", "error":"OOPS", "seeing":"TAKING A LOOK",
                  "connecting":"CONNECTING"}
        label = "MIC MUTED" if view.muted else labels.get(state, "RIGHT HERE")
        p.centre(label, 271, 2, eye if state in ("error","low_battery") else WHITE)
        detail = view.code[:48] if state == "error" else view.text[:48]
        if detail:
            p.centre(detail, 299, 1, dim)
        else:
            p.line(225, 302, 255, 302, 2, dim)

    def _card(self, p, view, eye, dim, bg):
        p.text("ON MY MIND", 24, 81, 1, dim)
        for cx in (423, 446):
            p.round_rect(cx-7, 77, 14, 20, 5, eye)
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
