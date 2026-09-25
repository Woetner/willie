"""eufy outdoor cameras (S12): the tool list, shared by the robot and the hub.

The cameras live on the home server (`homeserver/eufy-bridge/` + `hub/eufy.py`, eufy guest
account). Like reminders.py, this file only declares the tool; the robot forwards the call
to the hub over MQTT. A picture is only taken when Wouter asks (§13, 24 Sep).
"""
from __future__ import annotations

LABEL = "eufy buitencamera's (S12, on the home server)"
TIMEOUT_S = 75.0          # a battery camera wakes up first, then Gemini looks
ARCHIVE_TIMEOUT_S = 10.0  # camera_gebeurtenissen only reads the server's archive

# Set by tools/willie_voice.py when the MQTT bridge runs: hub_call(name, args, timeout) -> dict.
HUB_CALL = None

DECLARATIONS = [
    {
        "name": "buitencamera",
        "description": (
            "Kijk nu door een van de eufy-buitencamera's: Voordeur (voordeur + oprit), Garage (zijpad bij de garage), Achtertuin: de foto komt "
            "in Wouters app en je krijgt een beschrijving terug. Alleen als Wouter erom vraagt, bijv. "
            "'kijk op de oprit'. Zonder camera krijg je de lijst met namen."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "camera": {"type": "string", "description": "Voordeur, Garage of Achtertuin (de oprit = Voordeur)."},
                "waar_op_letten": {"type": "string", "description": "Waar je op moet letten, bijv. 'staat er een pakketje?'."},
            },
        },
    },
    {
        "name": "camera_gebeurtenissen",
        "description": (
            "Wat de buitencamera's zelf hebben gezien (S16 archief): personen, voertuigen, dieren, pakketjes, de "
            "deurbel, beweging, met tijd en camera, en wie er herkend is. Voor vragen als 'was er vandaag iemand "
            "bij de voordeur?' of 'is er een auto de oprit op gereden?'. Maakt geen nieuwe foto."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "camera": {"type": "string", "description": "Voordeur, Garage of Achtertuin; leeg = alle."},
                "soort": {"type": "string", "description": "persoon, vehicle, pet, package, doorbell, motion, of een ding als 'auto' of 'kat'; leeg = alles."},
                "uren": {"type": "number", "description": "Hoe ver terug, in uren (standaard 24)."},
            },
        },
    },
]
NAMES = {d["name"] for d in DECLARATIONS}


def forward(name: str, args: dict) -> dict:
    """On the robot: run the tool on the home server."""
    if HUB_CALL is None:
        return {"fout": "De thuisserver is niet verbonden, dus de buitencamera's kan ik nu niet zien."}
    return HUB_CALL(name, args or {}, ARCHIVE_TIMEOUT_S if name == "camera_gebeurtenissen" else TIMEOUT_S)


HANDLERS = {n: (lambda n: lambda **kw: forward(n, kw))(n) for n in NAMES}


def card() -> dict:
    return {"draait op": "thuisserver, via MQTT", "beeld": "alleen op verzoek"}
