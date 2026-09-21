"""Pictures on the face (toon_afbeelding): decode, scale, convert, draw, tool. Offline."""
import asyncio
import json

import pytest

from willie.face import picture
from willie.face.framebuffer import Framebuffer
from willie.face.renderer import Renderer
from willie.face.runtime import Face
from willie.voice import gemini_live


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


RED = bytes((255, 0, 0))


def solid(w, h, rgb=RED):
    return picture.Picture("Banaan (vrucht)", w, h, rgb * (w * h), "https://nl.wikipedia.org/wiki/Banaan")


def test_parse_ppm_with_comment():
    w, h, rgb = picture.parse_ppm(b"P6\n# djpeg\n2 1\n255\n" + RED + bytes((0, 0, 255)))
    assert (w, h) == (2, 1) and rgb == RED + bytes((0, 0, 255))
    with pytest.raises(RuntimeError):
        picture.parse_ppm(b"P6 2 2 255\n" + RED)


def test_fit_keeps_aspect_and_box():
    fitted = solid(400, 200).fit(432, 180)
    assert (fitted.width, fitted.height) == (360, 180)
    assert len(fitted.rgb) == 360 * 180 * 3 and fitted.rgb[:3] == RED
    small = solid(100, 100).fit(432, 180)           # small thumbnails are scaled up
    assert (small.width, small.height) == (180, 180)


def test_convert_rgb565():
    fb = Framebuffer.canvas(4, 4)
    assert fb.convert(RED) == (0xF800).to_bytes(2, "little")
    assert fb.convert(bytes((255, 255, 255))) == (0xFFFF).to_bytes(2, "little")


def test_show_image_draws_picture_and_tap_dismisses():
    fb = Framebuffer.canvas()
    face = Face([fb], clock=Clock(), autostart=False)
    assert face.show_image(solid(400, 200)) == {"getoond": "Banaan (vrucht)"}
    view = face.snapshot()
    assert view.state == "show" and view.image
    red = (0xF800).to_bytes(2, "little")
    centre = (142 * fb.stride) + 240 * 2                # middle of the picture box
    buffer = fb.back_buffer()
    Renderer().draw(buffer, view, face.clock(), {"brightness": 100})
    assert bytes(buffer.memory[centre:centre + 2]) != red   # eyelids still closed at t=0
    face.clock.now += 1                                 # past the eyelid reveal
    Renderer().draw(buffer, face.snapshot(), face.clock(), {"brightness": 100})
    assert bytes(buffer.memory[centre:centre + 2]) == red
    face.clock.now += 2
    face.pet()                                          # one tap returns to the face
    assert face.snapshot().state != "show"
    face.show("tekst")                                  # a text card drops the picture
    assert face.snapshot().image is None


def test_search_keeps_only_jpeg_lead_images(monkeypatch):
    reply = {"query": {"pages": [
        {"title": "Blauwe Banaan", "index": 2, "thumbnail": {"source": "https://u/x/Blaue.png/500px-Blaue.png"}},
        {"title": "Banaan (vrucht)", "index": 1, "thumbnail": {"source": "https://u/Bananavarieties.jpg?utm=1"}},
        {"title": "Banaan", "index": 3},
    ]}}
    monkeypatch.setattr(picture, "_get", lambda url, timeout=8.0: json.dumps(reply).encode())
    assert picture.search("banaan", "nl") == [("Banaan (vrucht)", "https://u/Bananavarieties.jpg?utm=1")]
    reply["query"]["pages"][1]["thumbnail"]["source"] = "https://u/Stepper.gif"
    assert picture.search("banaan", "nl") == []          # top hit has no photo: no guessing


def test_tool_puts_found_photo_on_face(monkeypatch):
    face = Face([Framebuffer.canvas()], clock=Clock(), autostart=False)
    monkeypatch.setattr(picture, "find", lambda subject, subject_en="": solid(387, 221))
    tools = {t.name: t for t in gemini_live.willie_tool_list(face)}
    result = asyncio.run(tools["toon_afbeelding"].handler({"onderwerp": "banaan"}))
    assert result["getoond"] == "Banaan (vrucht)" and face.snapshot().state == "show"

    def missing(subject, subject_en=""):
        raise RuntimeError("no photo found")
    monkeypatch.setattr(picture, "find", missing)
    result = asyncio.run(tools["toon_afbeelding"].handler({"onderwerp": "xyzzy"}))
    assert "fout" in result
