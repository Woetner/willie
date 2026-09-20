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
| `make flash MCU=esp32dev` | build + flash the firmware (`esp32s3` is the default) |
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
