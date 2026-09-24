"""His body as a skill (F3/F4): drive a short distance, turn, stop - through the core's
control socket, so every command passes safety.py and the firmware's own limits (D18).

Deliberately small steps: at most 1 m and one full turn per call, at the configured
drive.max_speed. "Rij naar de keuken" needs places and navigation (G1) and is not here.
Switch the whole skill off in the dashboard's Skills tab while the robot is on the desk.
"""
from __future__ import annotations

from willie import control

LABEL = "Lichaam - short moves and turns (through safety.py)"
MAX_M = 1.0
MAX_DEG = 360.0

DECLARATIONS = [
    {
        "name": "rijden",
        "description": (
            "Rij een klein stukje recht vooruit (positief) of achteruit (negatief), maximaal 1 meter. "
            "Alleen als Wouter vraagt om te rijden, dichterbij te komen of opzij te gaan. Je stopt "
            "zelf voor obstakels; zeg het als dat gebeurt."
        ),
        "parameters": {
            "type": "object",
            "properties": {"meter": {"type": "number", "description": "Afstand in meter, -1 tot 1."}},
            "required": ["meter"],
        },
    },
    {
        "name": "draaien",
        "description": (
            "Draai je hele lijf op de plek: positief = linksom, negatief = rechtsom, in graden "
            "(max 360). 'Draai je om' = 180 graden, 'kijk naar links' = 90."
        ),
        "parameters": {
            "type": "object",
            "properties": {"graden": {"type": "number", "description": "Graden, -360 tot 360."}},
            "required": ["graden"],
        },
    },
    {
        "name": "stilstaan",
        "description": "Stop meteen met rijden of draaien. Gebruik dit zodra Wouter 'stop' of 'ho' zegt.",
        "parameters": {"type": "object", "properties": {}},
    },
]


def rijden(meter: float) -> dict:
    meter = max(-MAX_M, min(MAX_M, float(meter)))
    return control.request("move", timeout=20, m=meter)


def draaien(graden: float) -> dict:
    graden = max(-MAX_DEG, min(MAX_DEG, float(graden)))
    return control.request("turn", timeout=20, deg=graden)


def stilstaan() -> dict:
    return control.request("stop", timeout=2)


HANDLERS = {"rijden": rijden, "draaien": draaien, "stilstaan": stilstaan}


def card() -> dict:
    state = control.request("state", timeout=0.3)
    if "fout" in state:
        return {"lichaam": "core antwoordt niet"}
    if state.get("state_age_s") is None:
        return {"lichaam": "geen ESP32"}
    return {"lichaam": ", ".join(state.get("estop") or []) or state.get("busy") or "klaar"}
