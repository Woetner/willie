"""Pinouts on the face (K1, D28): a chip drawing you can zoom and drag on the 3.5" panel.

The home server sends the pin table (hub/garage.py: names, functions, which side, dangers);
this file lays it out once in "world" units and draws the visible part at the current zoom
and pan. Drawing uses the face's own primitives (Painter), so it stays sharp at every zoom
and needs no imaging library on the Pi (D20).

    Layout(data)          world positions of the body, pins and labels (once per pinout)
    Pinout(layout, pin)   the view: zoom, centre, highlighted pin; zoom_step() / pan()
    draw(p, view, ...)    called by the renderer for the "pinout" face state

Zoom steps are fixed (fit, x2, x3 of fit): a double-tap goes to the next one around the
tapped point, a drag pans, "zoom op GPIO21" centres the pin at x2.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import font

PITCH = 22             # world units between two pins
STUB = 12              # pin leg outside the body
LABEL_CHARS = 17       # label room beside the body, in characters at text scale 1
LABEL_W = LABEL_CHARS * 6
BODY_MIN_W, BODY_MIN_H = 120, 66
VIEW_W, VIEW_H = 480, 284          # the picture box above the 36 px bottom bar
ZOOM_STEPS = (1.0, 2.0, 3.0)


def _norm(text) -> str:
    return re.sub(r"[^A-Z0-9]", "", font.normalise(str(text)))


@dataclass
class Pin:
    nr: int
    name: str
    function: str
    side: str
    danger: str
    corrected: bool
    x: float = 0.0          # where the leg meets the label (world)
    y: float = 0.0
    leg: tuple = ()          # (x, y, w, h) of the leg (world)

    def label(self) -> str:
        text = self.name
        if self.function and _norm(self.function) not in _norm(self.name):
            text += " " + self.function
        return font.normalise(text)


@dataclass
class Layout:
    title: str
    checked: int            # 2 = datasheet checked, 1 = functions only (board vs chip), 0 = not
    note: str
    pins: list[Pin]
    width: float = 0.0
    height: float = 0.0
    body: tuple = (0, 0, 0, 0)       # x, y, w, h (world)
    orientation: str = ""

    @classmethod
    def build(cls, data: dict) -> "Layout":
        pins = []
        for raw in data.get("pinnen") or []:
            side = str(raw.get("kant") or "links")
            pins.append(Pin(int(raw.get("nr") or len(pins) + 1), str(raw.get("naam") or "?"),
                            str(raw.get("functie") or ""), side if side in ("links", "rechts", "boven", "onder") else "links",
                            str(raw.get("gevaar") or ""), bool(raw.get("gecorrigeerd"))))
        sides = {s: [p for p in pins if p.side == s] for s in ("links", "rechts", "boven", "onder")}
        n_lr = max(len(sides["links"]), len(sides["rechts"]), 1)
        n_tb = max(len(sides["boven"]), len(sides["onder"]))
        body_w = max(BODY_MIN_W, (n_tb + 1) * PITCH)
        body_h = max(BODY_MIN_H, (n_lr + 1) * PITCH)
        pad_l = LABEL_W + STUB + 8 if sides["links"] else 16
        pad_r = LABEL_W + STUB + 8 if sides["rechts"] else 16
        # Top/bottom labels are staggered over 3 rows so neighbours never overlap.
        pad_t = STUB + 3 * 12 + 8 if sides["boven"] else 16
        pad_b = STUB + 3 * 12 + 8 if sides["onder"] else 16
        bx, by = pad_l, pad_t
        for i, pin in enumerate(sides["links"]):
            y = by + (i + 1) * body_h / (len(sides["links"]) + 1)
            pin.leg, pin.x, pin.y = (bx - STUB, y - 3, STUB, 6), bx - STUB - 4, y
        for i, pin in enumerate(sides["rechts"]):
            y = by + (i + 1) * body_h / (len(sides["rechts"]) + 1)
            pin.leg, pin.x, pin.y = (bx + body_w, y - 3, STUB, 6), bx + body_w + STUB + 4, y
        for i, pin in enumerate(sides["boven"]):
            x = bx + (i + 1) * body_w / (len(sides["boven"]) + 1)
            pin.leg, pin.x, pin.y = (x - 3, by - STUB, 6, STUB), x, by - STUB - 6 - (i % 3) * 12
        for i, pin in enumerate(sides["onder"]):
            x = bx + (i + 1) * body_w / (len(sides["onder"]) + 1)
            pin.leg, pin.x, pin.y = (x - 3, by + body_h, 6, STUB), x, by + body_h + STUB + 6 + (i % 3) * 12
        level = data.get("controle_niveau", 2 if data.get("gecontroleerd") else 0)
        return cls(font.normalise(data.get("naam") or "PINOUT"), int(level or 0),
                   str(data.get("controle") or ""), pins, pad_l + body_w + pad_r, pad_t + body_h + pad_b,
                   (bx, by, body_w, body_h), font.normalise(data.get("orientatie") or ""))

    def find(self, query) -> Pin | None:
        """A pin by number ("21"), name ("GPIO21", "gpio 21") or function ("SDA")."""
        q = _norm(query)
        if not q:
            return None
        if q.isdigit():
            # "21" on a board means GPIO21 more often than physical pin 21.
            for pin in self.pins:
                if _norm(pin.name) in (f"GPIO{q}", f"IO{q}", f"D{q}", f"P{q}", f"PIN{q}"):
                    return pin
            for pin in self.pins:
                if pin.nr == int(q):
                    return pin
        for pin in self.pins:
            if _norm(pin.name) == q:
                return pin
        for pin in self.pins:
            if q in _norm(pin.function).split() or q == _norm(pin.function) or q in _norm(pin.label()):
                return pin
        return None

    def fit(self) -> float:
        return min(VIEW_W / self.width, VIEW_H / self.height)


@dataclass
class Pinout:
    """The live view state; the Face owns one while a pinout is on screen."""
    layout: Layout
    highlight: Pin | None = None
    step: int = 0
    cx: float = 0.0
    cy: float = 0.0

    def __post_init__(self):
        self.cx, self.cy = self.layout.width / 2, self.layout.height / 2

    @property
    def zoom(self) -> float:
        return self.layout.fit() * ZOOM_STEPS[self.step]

    def focus(self, query) -> Pin | None:
        pin = self.layout.find(query)
        if pin:
            self.highlight = pin
            self.step = max(self.step, 1)
            self.cx, self.cy = pin.x, pin.y
            self._clamp()
        return pin

    def zoom_step(self, sx: float | None = None, sy: float | None = None) -> None:
        """Double-tap: next zoom step, keeping the tapped point (screen coords) in place."""
        old = self.zoom
        self.step = (self.step + 1) % len(ZOOM_STEPS)
        if self.step == 0:
            self.cx, self.cy = self.layout.width / 2, self.layout.height / 2
            return
        if sx is not None and sy is not None:
            wx, wy = self.to_world(sx, sy, old)
            new = self.zoom
            self.cx, self.cy = wx - (sx - VIEW_W / 2) / new, wy - (sy - VIEW_H / 2) / new
        self._clamp()

    def pan(self, dx: float, dy: float) -> None:
        """Drag by (dx, dy) screen pixels: the drawing follows the finger."""
        self.cx -= dx / self.zoom
        self.cy -= dy / self.zoom
        self._clamp()

    def to_world(self, sx: float, sy: float, zoom: float | None = None) -> tuple[float, float]:
        z = zoom or self.zoom
        return self.cx + (sx - VIEW_W / 2) / z, self.cy + (sy - VIEW_H / 2) / z

    def _clamp(self) -> None:
        z = self.zoom
        half_w, half_h = VIEW_W / 2 / z, VIEW_H / 2 / z
        w, h = self.layout.width, self.layout.height
        self.cx = w / 2 if half_w * 2 >= w else max(half_w, min(w - half_w, self.cx))
        self.cy = h / 2 if half_h * 2 >= h else max(half_h, min(h - half_h, self.cy))

    def frozen(self) -> tuple:
        """What the renderer needs, as plain values (the View is copied every frame)."""
        return (self.layout, self.highlight, self.zoom, round(self.cx, 1), round(self.cy, 1))


# ======================================================================== drawing
def draw(p, frozen: tuple, colours: dict) -> None:
    """Draw the visible part of the pinout in the picture box. `colours`: eye, dim, bg,
    white, amber, red (already mixed by the renderer)."""
    layout, highlight, zoom, cx, cy = frozen
    eye, dim, bg = colours["eye"], colours["dim"], colours["bg"]
    white, amber, red = colours["white"], colours["amber"], colours["red"]
    scale = 1 if zoom < 1.7 else 2 if zoom < 2.7 else 3

    def sx(x):
        return (x - cx) * zoom + VIEW_W / 2

    def sy(y):
        return (y - cy) * zoom + VIEW_H / 2

    def box(x, y, w, h, c):
        """A world rectangle clipped to the picture box."""
        x0, y0, x1, y1 = max(0, sx(x)), max(0, sy(y)), min(VIEW_W, sx(x + w)), min(VIEW_H, sy(y + h))
        if x1 > x0 and y1 > y0:
            p.rect(x0, y0, max(1, x1 - x0), max(1, y1 - y0), c)

    def text(t, x, y, c, s=scale):
        """Text at screen (x, y) top-left, only if it fits the box entirely."""
        w, h = len(t) * 6 * s, 7 * s
        if x >= 0 and y >= 0 and x + w <= VIEW_W and y + h <= VIEW_H and t:
            p.text(t, x, y, s, c)

    bx, by, bw, bh = layout.body
    box(bx, by, bw, bh, dim)
    box(bx + 2, by + 2, bw - 4, bh - 4, bg)
    # Pin 1 mark: a dot in the body's top-left corner, like on the package.
    box(bx + 8, by + 8, 6, 6, dim)
    if zoom <= layout.fit() * 1.01:          # zoomed in, the pin numbers need the room
        name = layout.title[: max(1, int(bw * zoom / (6 * scale)) - 2)]
        text(name, sx(bx + bw / 2) - len(name) * 3 * scale, sy(by + bh / 2) - 3.5 * scale, white)
    room = max(3, int(LABEL_W * zoom / (6 * scale)))
    for pin in layout.pins:
        hot = pin is highlight
        leg = amber if hot else red if pin.danger else eye
        box(*pin.leg, leg)
        label = pin.label()[:room]
        c = amber if hot else white
        px, py = sx(pin.x), sy(pin.y)
        if pin.side == "links":
            text(label, px - len(label) * 6 * scale, py - 3.5 * scale, c)
        elif pin.side == "rechts":
            text(label, px, py - 3.5 * scale, c)
        else:
            short = label[: max(3, int(PITCH * 3 * zoom / (6 * scale)) - 1)]
            text(short, px - len(short) * 3 * scale, py - 3.5 * scale, c)
        if zoom >= 1.2:
            # The pin number just inside the body, next to its leg.
            nr = str(pin.nr)
            if pin.side == "links":
                nx, ny = sx(bx) + 4, py - 3.5
            elif pin.side == "rechts":
                nx, ny = sx(bx + bw) - 4 - len(nr) * 6, py - 3.5
            elif pin.side == "boven":
                nx, ny = px - len(nr) * 3, sy(by) + 4
            else:
                nx, ny = px - len(nr) * 3, sy(by + bh) - 11
            text(nr, nx, ny, amber if hot else dim, 1)


def detail(frozen: tuple) -> str:
    """The bottom line: the highlighted pin in full, or the part's name."""
    layout, highlight = frozen[0], frozen[1]
    if highlight is None:
        return layout.title
    extra = f" - {font.normalise(highlight.danger)}" if highlight.danger else ""
    return f"{highlight.nr} {highlight.label()}{extra}"
