# WILL-E — robot code

Master plan: `../WILL-E.md`. Mac = development, Pi 3 A+ (`willie.local`) = runtime.

## Layout (grows phase by phase, see WILL-E.md §5.3)
```
willie/            Python package (asyncio, one process)
  core.py          start-up + task supervisor
  config/          settings loader (schema + live YAML, hot reload)
  dashboard/       FastAPI + static web UI (port 8080)
  hello.py         deploy-loop test (A5)
config/
  schema.yaml      every setting: type, default, limits, unit, help
  willie.yaml      default values (the live copy lives on the Pi, see below)
systemd/           willie.service, pigpiod.service
tools/             pi_setup.sh (A4), install_service.sh, bench scripts later
```

## Everyday commands (on the Mac, in this folder)
| | |
|---|---|
| `make deploy` | rsync to the Pi + restart WILL-E |
| `make logs` | follow the log |
| `make deps` | after changing requirements.txt |
| `make pull-config` | copy dashboard-changed settings back into `config/willie.yaml` |
| `make run-local` | run on the Mac → http://localhost:8080 |

## Settings
`config/schema.yaml` declares every setting. The dashboard form is generated from it.
The live values on the Pi are in `~/.config/willie/willie.yaml` (created from
`config/willie.yaml` on first start), so a deploy never overwrites what you tuned in the dashboard.
Hand edits of the live file are picked up within 2 s.

## Secrets
API keys only in `.env` (git-ignored, but synced to the Pi by `make deploy`). Template: `.env.example`.
