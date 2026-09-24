"""K1 pinouts on the face: layout, zoom/pan, touch gestures, drawing."""
import os
import time

from willie.face.framebuffer import Framebuffer
from willie.face.pinout import Layout, Pinout, VIEW_H, VIEW_W
from willie.face.renderer import Renderer
from willie.face.runtime import Face, Touch

LEFT = ["EN", "VP", "VN", "D34", "D35", "D32", "D33", "D25", "D26", "D27", "D14", "D12", "D13", "GND", "VIN"]
RIGHT = ["D23", "D22", "TX0", "RX0", "D21", "D19", "D18", "D5", "TX2", "RX2", "D4", "D2", "D15", "GND", "3V3"]
ESP32 = {
    "naam": "ESP32 DevKit V1 (30-pin)", "soort": "board", "gecontroleerd": True,
    "pinnen": [{"nr": i + 1, "naam": n, "kant": "links", "functie": "ADC1_CH6" if n == "D34" else ""}
               for i, n in enumerate(LEFT)]
    + [{"nr": 16 + i, "naam": n, "kant": "rechts", "functie": {"D21": "SDA", "D22": "SCL"}.get(n, ""),
        "gevaar": "boot-strap: laag bij opstarten = flash-modus" if n == "D2" else ""}
       for i, n in enumerate(RIGHT)],
}
NE555 = {"naam": "NE555", "pinnen": [
    {"nr": 1, "naam": "GND", "kant": "links"}, {"nr": 2, "naam": "TRIG", "kant": "links"},
    {"nr": 3, "naam": "OUT", "kant": "links"}, {"nr": 4, "naam": "RESET", "kant": "links"},
    {"nr": 8, "naam": "VCC", "kant": "rechts"}, {"nr": 7, "naam": "DISCH", "kant": "rechts"},
    {"nr": 6, "naam": "THRES", "kant": "rechts"}, {"nr": 5, "naam": "CTRL", "kant": "rechts"}]}


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_layout_fits_and_finds_pins():
    layout = Layout.build(ESP32)
    view = Pinout(layout)
    assert layout.width * view.zoom <= VIEW_W + 0.01 and layout.height * view.zoom <= VIEW_H + 0.01
    assert layout.find("SDA").name == "D21"
    assert layout.find("gpio 21") is None or layout.find("gpio 21").name == "D21"
    assert layout.find("21").name == "D21"          # "21" = D21, not physical pin 21
    assert layout.find("3v3").nr == 30
    left = [p for p in layout.pins if p.side == "links"]
    assert all(a.y < b.y for a, b in zip(left, left[1:]))   # top to bottom


def test_focus_zoom_and_pan_stay_inside():
    view = Pinout(Layout.build(ESP32))
    fit = view.zoom
    assert view.focus("SDA").name == "D21"
    assert view.zoom == fit * 2
    x, y = view.cx, view.cy
    view.pan(-5000, -5000)                           # dragged far away: clamped to the drawing
    assert view.cx <= view.layout.width and view.cy <= view.layout.height
    view.pan(5000, 5000)
    assert view.cx >= 0 and view.cy >= 0 and (view.cx, view.cy) != (x, y)
    view.zoom_step(240, 142)
    assert view.zoom == fit * 3
    view.zoom_step()
    assert view.zoom == fit and view.cx == view.layout.width / 2   # back to the whole chip


def test_zoom_keeps_tapped_point_in_place():
    view = Pinout(Layout.build(NE555))
    before = view.to_world(100, 100)
    view.zoom_step(100, 100)
    after = view.to_world(100, 100)
    assert abs(before[0] - after[0]) < 20 and abs(before[1] - after[1]) < 20


def test_face_pinout_state_and_gestures():
    face = Face(clock=Clock(), autostart=False)
    result = face.show_pinout(ESP32, pin="SDA")
    assert result["pin"].startswith("20 D21")
    v = face.snapshot()
    assert v.state == "pinout" and v.pinout[1].name == "D21"
    zoom = face._pinout.zoom
    face.pinout_gesture(("tap", 200, 150), 100.0)
    face.pinout_gesture(("tap", 205, 152), 100.2)        # double-tap: next step
    assert face._pinout.zoom > zoom
    face.pinout_gesture(("drag", 30, 0), 100.5)
    face.pinout_gesture(("long", 200, 150), 101.0)      # long press closes it
    assert face.snapshot().state != "pinout"
    face.show_pinout(NE555)
    face.show("iets anders")                            # a new card replaces the pinout
    assert face.snapshot().state == "show"


def test_draws_on_a_canvas_at_every_zoom():
    face = Face(clock=Clock(), autostart=False)
    face.show_pinout(ESP32, pin="D2")
    canvas = Framebuffer.canvas()
    renderer = Renderer()
    for _ in range(3):
        rows = renderer.draw(canvas, face.snapshot(), 100.0, {})
        assert rows >= 0
        face.pinout_gesture(("tap", 240, 140), 1.0)
        face.pinout_gesture(("tap", 240, 140), 1.1)
    started = time.perf_counter()
    face.pinout_gesture(("drag", 15, 10), 2.0)
    renderer.draw(canvas, face.snapshot(), 101.0, {})
    assert time.perf_counter() - started < 1.0


def test_touch_turns_raw_events_into_drag_and_tap():
    r, w = os.pipe()
    os.set_blocking(r, False)
    touch = Touch.__new__(Touch)
    touch.fd, touch.pending, touch.down, touch.gestures = r, b"", False, []
    touch.calibration = {"touch_x_min": 0, "touch_x_max": 4800, "touch_y_min": 0, "touch_y_max": 3200}
    ev = Touch.EVENT.pack

    def frame(x, y):
        return ev(0, 0, 3, 0, x) + ev(0, 0, 3, 1, y) + ev(0, 0, 0, 0, 0)

    os.write(w, ev(0, 0, 1, 330, 1) + frame(1000, 1000) + frame(1500, 1000) + frame(2000, 1500))
    assert not touch.poll(now=10.0)
    drags = [g for g in touch.gestures if g[0] == "drag"]
    assert drags and round(sum(g[1] for g in drags)) == 100 and round(sum(g[2] for g in drags)) == 50
    os.write(w, ev(0, 0, 1, 330, 0))
    assert touch.poll(now=10.3) and touch.gestures == [("release",)]
    os.write(w, ev(0, 0, 1, 330, 1) + frame(2400, 1600))
    touch.poll(now=11.0)
    os.write(w, ev(0, 0, 1, 330, 0))
    assert touch.poll(now=11.1) and touch.gestures[0][0] == "tap"
    os.close(r)
    os.close(w)
