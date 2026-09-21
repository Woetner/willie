# WILL-E face (D6)

The face is a standard-library Python renderer for the existing 480×320 ILI9486
framebuffer. No pygame, Pillow, browser, additional service, or cloud connection is
needed on the Pi. The renderer draws in RAM first and presents only changed rows.

## Try it on the Mac

From the `willie` repository, run `make face-preview`, then open
<http://127.0.0.1:8765>. The preview renders the **same Python drawing code** into
an RGB565 memory buffer and serves PNG frames to a local browser. Its PNG encoder
and web server are desktop tooling; the Pi runtime does not import them.

Choose an expression or press **Play sequence**. Tap the face (or Space) to pet it;
F opens fullscreen; keys 1–9 choose the first nine expressions. Use Show on screen
for a short number or a paged text card. Long text advances every six seconds;
tapping turns the page. A single-page card dismisses on tap.

All status and voice controls in the preview are simulated. It never opens a
microphone, camera, robot connection, or AI API. Settings there affect the preview
only. Unknown battery/connection values are drawn as unknown, including on the Pi.

## Run on the Pi

After copying the code to the Pi, run from `~/willie`:

```sh
make face-demo
```

This is a 60-second **face-only** demonstration with synthetic speech amplitude.
It does not open audio or camera hardware. It shows every expression, handles
screen taps, and reports peak process RSS, render-loop fps, draw time, and changed
rows. Stop other face consoles before running it so only one process owns the
panel. It prefers the ILI9486 panel over HDMI.

For an actual conversation, use the existing `make live-talk` or `make voice-pi`
commands on the Mac. `gemini_live.session()` owns one animated face and connects
voice events to it. The hands-free runner retains one face between sessions so
idle blinking continues while waiting for the local wake word. The older
`tools/willie_console.py` reuses the renderer too; its blocking TTS path does not
provide PCM envelopes, so volume-driven eye bounce is specific to the live path.

## Behaviour and integration

- States: idle, curious, listening, thinking, talking, happy, sad, surprised,
  sleep, low battery, error, seeing, show, connecting.
- Live outgoing PCM supplies a bounded queue of 20 ms RMS envelopes, scheduled at
  the speaker's estimated playback time. Server turn completion waits for that
  audio tail; interruptions clear all pending motion immediately.
- Capture start/stop events control the microphone indicator. A connection alone
  does not claim the mic is active. Camera tool activity is indicated throughout
  capture/upload, including worker cleanup after cancellation.
- The `toon(tekst)` voice tool updates the existing face rather than opening a
  competing framebuffer writer. Cards keep mic/camera status visible.
- Touch releases trigger a short happy expression and sparkle. Low battery and
  error states retain priority. Petting needs no coordinate calibration because
  the whole panel is a single target.
- Existing `face.eye_style`, `eye_color`, `bg_color`, `brightness`, `blink_rate`
  settings are honored. `face.fps` adds a 5–30 fps limit (default 25). The robot
  reloads these from the existing Config store every two seconds. Brightness
  scales rendered colours, not the panel's physical backlight.

Public integration API:

```python
from willie.face.runtime import Face

face = Face.open()  # starts an animation thread, in this process
try:
    face.set_state("curious")
    face.indicators(battery=64, charging=False)  # supply measured telemetry
    face.indicators(mic=True, camera=False)      # report actual device activity
    face.show("M3 = 0.5 MM")
    face.pet()
    # face.event(kind, detail): adapter/capture events
    # face.audio(pcm, rate=24000, starts_at=playback_time)
finally:
    face.close()  # joins thread, releases touch/fb, leaves inactive indicators
```

`indicators(muted=True)` reports a mute action performed by the audio owner; it
**does not mute hardware**. Battery telemetry is not wired to an INA219 percentage
source yet; absent a caller-provided measurement, it stays unknown. `connected`
indicates the live AI session, not a Wi-Fi-strength measurement. This slice covers
text cards; arbitrary photos, URLs, and image decoding remain future `show()` work.

## Validation and remaining physical checks

Run `make face-test` for the face and existing offline voice-adapter regressions.
The voice-adapter tests use a local mock WebSocket server, with no real API calls.
For memory-only drawing on the Mac: `python3 tools/bench/face.py --headless`.

2026-09-21 development measurement (Mac, 5 seconds, idle + curious): 23.0 render-loop
fps, mean draw 2.32 ms, worst draw 4.08 ms, 26 changed rows/frame, peak process RSS
23.45 MiB. This includes the macOS Python process and is **not** a Pi RSS result.
No runtime dependency was added. The Pi's ≤15 MB face budget remains unverified.

The Pi was unreachable via SSH (`No route to host`) during this implementation.
D6 remains unticked until a one-minute video verifies the real screen with live
voice, and Pi RSS/panel throughput have been measured. The render-loop rate is
not a measurement of SPI panel updates. Gate G2 and its full-system test remain
separate work.
