#!/usr/bin/env python3
"""D6 face demo / RAM measurement. No microphone, camera, cloud, motors or new service."""
import argparse
import array
import math
import resource
import sys
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from willie.face.framebuffer import Framebuffer
from willie.face.renderer import STATES
from willie.face.runtime import Face


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds",type=float,default=60)
    parser.add_argument("--headless",action="store_true",help="Draw into RAM (Mac/CI), not the physical panel")
    parser.add_argument("--state",choices=STATES,help="Hold one expression; otherwise demonstrate all states")
    args=parser.parse_args()
    face=Face([Framebuffer.canvas()]) if args.headless else Face.open()
    start=time.monotonic()
    previous=None
    print("D6 offline face demo; status data and speech envelope are SIMULATED",flush=True)
    print("Tap to pet / turn a text page. Ctrl-C exits.",flush=True)
    try:
        while time.monotonic()-start < args.seconds:
            elapsed=time.monotonic()-start
            state=args.state or STATES[int(elapsed/4)%len(STATES)]
            if state != previous:
                face.dismiss()
                face.set_state(state, code="DEMO E01" if state=="error" else "")
                face.indicators(battery=15 if state=="low_battery" else 72,
                                charging=state=="sleep", mic=False, camera=False, connected=None)
                if state=="show":
                    face.show("M3 BOLT\nPITCH = 0.5 MM\nDRILL = 2.5 MM")
                previous=state
                print(f"{elapsed:4.0f}s {state}",flush=True)
            if state=="talking":
                pcm=array.array("h", (int(9000*math.sin(i*.11)*(.5+.5*math.sin(elapsed*8))) for i in range(960)))
                if sys.byteorder!="little":
                    pcm.byteswap()
                face.audio(pcm.tobytes(),24000)
            if face.failure:
                raise RuntimeError(f"renderer failed: {face.failure}")
            time.sleep(.04)
    except KeyboardInterrupt:
        pass
    finally:
        face.close()
    elapsed=time.monotonic()-start
    usage=resource.getrusage(resource.RUSAGE_SELF)
    rss=usage.ru_maxrss/(1024*1024 if sys.platform=="darwin" else 1024)
    frames=max(1,face.frames)
    print(f"render loop {face.frames/elapsed:.1f} fps; mean draw {face.render_seconds/frames*1000:.2f} ms; "
          f"worst draw {face.max_render_seconds*1000:.2f} ms; changed rows/frame {face.dirty_rows/frames:.1f}")
    print(f"peak process RSS {rss:.2f} MiB ({sys.platform}); drawing CPU cost approx {face.render_seconds/elapsed*100:.1f}% of one core")
    print("Render-loop fps is NOT measured panel fps. D6 needs a 1-minute video on the actual screen.")


if __name__=="__main__":
    main()
