#!/usr/bin/env python3
"""Offline browser preview of the real Python framebuffer renderer. No mic, camera or API."""
from __future__ import annotations
import argparse
import json
import math
import struct
import sys
import time
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from willie.face.framebuffer import Framebuffer
from willie.face.renderer import STATES, Renderer
from willie.face.runtime import Face

# Desktop only; nothing in the robot's runtime imports this PNG encoder.
PIXELS = [bytes(((v >> 11)*255//31, ((v >> 5)&63)*255//63, (v&31)*255//31)) for v in range(65536)]


def png(frame):
    raw = bytearray()
    for y in range(frame.height):
        raw.append(0)
        start = y*frame.stride
        for (pixel,) in struct.iter_unpack("<H", frame.memory[start:start+frame.width*2]):
            raw.extend(PIXELS[pixel])
    def chunk(tag, data):
        return struct.pack(">I",len(data))+tag+data+struct.pack(">I",zlib.crc32(tag+data)&0xffffffff)
    return (b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",struct.pack(">IIBBBBB",frame.width,frame.height,8,2,0,0,0))
            +chunk(b"IDAT",zlib.compress(raw,1))+chunk(b"IEND",b""))


class Preview:
    def __init__(self):
        self.face = Face(autostart=False)
        self.renderer = Renderer()
        self.frame = Framebuffer.canvas()
        self.settings = {"eye_color":"#39d0ff", "brightness":90, "blink_rate":12, "eye_style":"round"}
        self.demo = False
        self.demo_index = -1
        self.started = time.monotonic()
        self.level = .65

    def update(self, data):
        if "demo" in data:
            self.demo = bool(data["demo"])
            self.started = time.monotonic()
            self.demo_index = -1
        if "state" in data:
            state = data["state"]
            if state not in STATES:
                raise ValueError("unknown state")
            self.face.dismiss()
            self.face.set_state(state)
            if state == "show":
                self.face.show(data.get("text") or "M3 BOLT\nPITCH = 0.5 MM\nDRILL = 2.5 MM")
        if data.get("pet"):
            self.face.pet()
        if "text" in data:
            self.face.show(data["text"])
        for key in ("mic", "camera", "muted", "connected", "charging", "battery"):
            if key in data:
                self.face.indicators(**{key:data[key]})
        if "level" in data:
            self.level = max(0,min(1,float(data["level"])))
        if data.get("eye_style") in ("round","visor","pixel"):
            self.settings["eye_style"] = data["eye_style"]
        if "eye_color" in data:
            self.settings["eye_color"] = str(data["eye_color"])
        if "brightness" in data:
            self.settings["brightness"] = max(5,min(100,int(data["brightness"])))

    def render(self):
        now = time.monotonic()
        if self.demo:
            sequence = ("idle","curious","listening","thinking","talking","happy","surprised","seeing","show","low_battery","sleep","error")
            index = int((now-self.started)//4)%len(sequence)
            if index != self.demo_index:
                self.demo_index = index
                state = sequence[index]
                self.update({"state":state, "mic":state in ("listening","thinking","talking"),
                             "camera":state == "seeing", "muted":False,
                             "battery":15 if state == "low_battery" else 72,
                             "connected":state not in ("error","sleep"), "charging":state == "sleep"})
        view = self.face.snapshot(now)
        if view.state == "talking":
            view.level = self.level*max(0, math.sin(now*11)*.5+.5)*(.5+.5*abs(math.sin(now*2)))
        self.view = view
        self.renderer.draw(self.frame,view,now,self.settings)
        return png(self.frame),view.state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port",type=int,default=8765)
    parser.add_argument("--snapshot",type=Path,help="Write one PNG and exit")
    parser.add_argument("--state",choices=STATES,default="idle")
    args = parser.parse_args()
    preview = Preview()
    preview.update({"state":args.state})
    if args.snapshot:
        args.snapshot.parent.mkdir(parents=True,exist_ok=True)
        args.snapshot.write_bytes(preview.render()[0])
        return
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def send(self,status,kind,body,headers=None):
            self.send_response(status)
            self.send_header("Content-Type",kind)
            self.send_header("Content-Length",str(len(body)))
            self.send_header("Cache-Control","no-store")
            for k,v in (headers or {}).items():
                self.send_header(k,v)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):
                pass
        def do_GET(self):
            if self.path == "/":
                self.send(200,"text/html; charset=utf-8",(REPO/"tools/face_preview.html").read_bytes())
            elif self.path.split("?")[0] == "/frame.png":
                data,state = preview.render()
                self.send(200,"image/png",data,{"X-Face-State":state, "X-Face-Status":json.dumps({k:getattr(preview.view,k) for k in ("mic","camera","muted","charging","battery","connected")})})
            else:
                self.send(404,"text/plain",b"Not found")
        def do_POST(self):
            if self.path != "/control":
                self.send(404,"text/plain",b"Not found")
                return
            try:
                if self.headers.get("Content-Type") != "application/json":
                    raise ValueError("JSON required")
                size = int(self.headers.get("Content-Length","0"))
                if not 0 < size <= 8192:
                    raise ValueError("invalid request size")
                data = json.loads(self.rfile.read(size))
                if not isinstance(data,dict):
                    raise ValueError("object required")
                preview.update(data)
                self.send(200,"application/json",b'{"ok":true}')
            except (ValueError,TypeError) as exc:
                self.send(400,"application/json",json.dumps({"error":str(exc)}).encode())
    server = HTTPServer(("127.0.0.1",args.port),Handler)
    print(f"WILL-E face preview: http://127.0.0.1:{server.server_port} — offline, no hardware",flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        preview.face.close()


if __name__ == "__main__":
    main()
