# WILL-E — robot code

Master plan: `../WILL-E.md`. Mac = development, Pi 3 A+ (`willie.local`) = runtime,
ESP32 = real-time co-processor (D18).

## Layout (grows phase by phase, see WILL-E.md §5.3)
```
willie/              Python package (asyncio)
  core.py            always-on process: config hot reload, MCU link, state.json
  config/            settings loader (schema + live YAML, hot reload)
  hal/proto.py       Pi <-> MCU line protocol (CRC-8 per line)
  hal/link.py        MCU serial link: ping/pong round-trip, stats (A9)
  dashboard/         FastAPI + static web UI, its own ON-DEMAND process (D20)
  hello.py           deploy-loop test (A5)
config/
  schema.yaml        every setting: type, default, limits, unit, help
  willie.yaml        default values (the live copy lives on the Pi, see below)
firmware/            ESP32 PlatformIO project (A9: blink + ping/pong)
systemd/             willie.service (MemoryMax), willie-dashboard.socket/.service
tools/               pi_setup.sh (A4), os_diet.sh + ram.sh (A8), fake_mcu.py, install_service.sh
```

## Everyday commands (on the Mac, in this folder) — `make help` for all
| | |
|---|---|
| `make deploy` | rsync to the Pi + restart WILL-E |
| `make ram` | per-process RAM table from the Pi (budget: WILL-E.md §5.5) |
| `make link-test` | 200 pings Pi → ESP32, round-trip times |
| `make ask-camera` | on the Pi keyboard: capture one JPEG and ask Gemini about it |
| `make flash` | build + flash the ESP32 firmware (30-pin WROOM DevKit) |
| `make bench-mcu T=servo` | B9–B12 bench tests over the link: `servo`, `sensors`, `io`, `motors`, `watch` |
| `make logs` | follow the core log |
| `make pull-config` | copy dashboard-changed settings back into `config/willie.yaml` |
| `make run-local` | core + dashboard + fake MCU on the Mac → http://localhost:8080 |

## Processes on the Pi
- `willie.service` — the core, always on, `MemoryMax=128M`.
- `willie-dashboard.socket` — systemd holds port 8080; the first browser visit starts
  `willie-dashboard.service`, which exits after `dashboard.idle_minutes` without requests.
  The two talk through files only: live settings YAML, `~/.local/share/willie/willie.log`, `state.json`.

## Settings
`config/schema.yaml` declares every setting. The dashboard form is generated from it.
The live values on the Pi are in `~/.config/willie/willie.yaml` (created from
`config/willie.yaml` on first start), so a deploy never overwrites what you tuned in the dashboard.
Changes are picked up by the core within 2 s.

## MCU wiring for A9 (3.3 V logic on both sides — no level shifter)
| Pi 3 A+ | ESP32-S3 | ESP32 DevKit (bench) |
|---|---|---|
| GPIO14 TXD (pin 8) | GPIO18 RX | GPIO16 RX2 |
| GPIO15 RXD (pin 10) | GPIO17 TX | GPIO17 TX2 |
| GND (pin 6) | GND | GND |
Power the ESP32 from the Mac's USB while flashing/testing.

## Secrets
API keys live only in `~/willie/.env` on the Pi; it is git-ignored and explicitly
preserved by `make deploy`. Template: `.env.example`.

## Camera question (bench prototype)
With the camera connected and `GEMINI_API_KEY` set in `~/willie/.env`, run from the
Pi's attached keyboard:

```bash
cd ~/willie
.venv/bin/python tools/ask_camera.py
```

It asks for a question, captures one 1024×768 JPEG with `rpicam-still`, sends that
image and question to Gemini, and prints the answer. The image is deleted afterwards.
Use `--save ~/picture.jpg` to keep it. This is an on-demand tool: no camera or AI
process remains running afterwards.

## Voice + face console (bench prototype)
On the Pi, run:

```bash
cd ~/willie
.venv/bin/python tools/willie_console.py
```

The face renders directly to every available `/dev/fb*` device and listens through
the I2S microphone. Say **“Hey Willie”** followed by a question, or say the wake
phrase by itself and wait for the `YES?` face before asking. Gemini answers through
the speaker. Ordinary questions use audio only; a photo is captured only for a
request such as “look at this” or “what is in front of you.” Use `Ctrl+C` to exit.

This temporary bench wake detector routes speech clips through Gemini while the
console is visibly listening. It is not the final always-on wake path: D2 keeps
“Hey Willie” local on the Pi (D19), so idle room audio never goes to the cloud.

The older typed camera console is still available with:

```bash
.venv/bin/python tools/willie_console.py --keyboard
```

Install the short launch command once from the Mac with `make face-install`; then
the Pi keyboard needs only `willie` and Enter.

## Voice bridge (temporary, Mac-only)
Talk to WILL-E with the MacBook's microphone and speakers while the robot has no
audio hardware of its own. From the Mac:

```bash
make voice
```

Press Enter to start recording, Enter again to stop. The WAV is piped over SSH to
`tools/ask_voice.py` on the Pi. Gemini answers audio-only questions directly and
asks the Pi for one camera still only when the question needs vision; the answer is
printed and read aloud by the built-in `say` command.

The split is deliberate: the Mac only records and speaks, so the **API key never
leaves the Pi**. `sounddevice` is installed from `requirements-mac.txt` into the
Mac's `.venv` only — the Pi never sees it (D21). macOS will ask for microphone
permission for your terminal on the first run.

This is a bench bridge, not the robot's voice. The real path is the on-robot
realtime speech-to-speech adapter chosen at Gate G1 (D9).
