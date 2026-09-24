"""Bambu Lab P1S (S11, H2): the tool list, shared by the robot and the hub.

The printer lives on the home server (`homeserver/hub/bambu.py`): it holds the one MQTT
connection to the printer, sends the push when a print finishes or fails, and runs the
"keep watching" checks while the Pi sleeps. Like reminders.py, this file is the single
source of the declarations; the robot forwards the calls to the hub over MQTT.

Read-only by decision (§13, 24 Sep): nothing here can start, pause or stop a print.
"""
from __future__ import annotations

LABEL = "Bambu P1S printer (S11, on the home server, read-only)"
TIMEOUT_S = 40.0          # printer_foto asks Gemini about the picture

# Set by tools/willie_voice.py when the MQTT bridge runs: hub_call(name, args, timeout) -> dict.
HUB_CALL = None

DECLARATIONS = [
    {
        "name": "printer_status",
        "description": (
            "De status van de 3D-printer (Bambu P1S): bezig of niet, welk bestand, voortgang, "
            "resterende tijd, laag, temperaturen en fouten. Gebruik dit bij elke vraag over de printer."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "printer_foto",
        "description": (
            "Kijk met de camera in de printer: de foto komt in Wouters app en je krijgt een "
            "beschrijving terug. Gebruik dit als hij wil zien hoe de print eruitziet."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "waar_op_letten": {"type": "string", "description": "Waar je op moet letten, bijv. 'is de eerste laag goed?'."},
            },
        },
    },
    {
        "name": "printer_bewaak",
        "description": (
            "Blijf de lopende print bewaken: elke paar minuten een foto bekijken en Wouter een "
            "melding sturen als de print mislukt (spaghetti, losgeraakt, klodder). Alleen als hij "
            "daarom vraagt. Stopt vanzelf als de print klaar is."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "aan": {"type": "boolean", "description": "true = start bewaken, false = stop."},
                "minuten": {"type": "integer", "description": "Om de hoeveel minuten kijken, 5-60. Standaard 10."},
            },
            "required": ["aan"],
        },
    },
]
NAMES = {d["name"] for d in DECLARATIONS}


def forward(name: str, args: dict) -> dict:
    """On the robot: run the tool on the home server."""
    if HUB_CALL is None:
        return {"fout": "De thuisserver is niet verbonden, dus de printer kan ik nu niet zien."}
    return HUB_CALL(name, args or {}, TIMEOUT_S)


HANDLERS = {n: (lambda n: lambda **kw: forward(n, kw))(n) for n in NAMES}


def card() -> dict:
    return {"draait op": "thuisserver, via MQTT", "rechten": "alleen lezen"}
