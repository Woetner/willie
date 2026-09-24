"""General watcher (companion app step 3): "tell me when ...", run on the home server.

Like eufy.py this file only declares the tools; the robot forwards the call to the hub over
MQTT and the hub (`homeserver/hub/watch.py`) does the watching, so a watch keeps running
while he sleeps, drives or is switched off. Each running watch is a card in the app and an
icon on his face.
"""
from __future__ import annotations

LABEL = "Bewaken: meldt als iets gebeurt (on the home server)"
TIMEOUT_S = 20.0

# Set by tools/willie_voice.py when the MQTT bridge runs: hub_call(name, args, timeout) -> dict.
HUB_CALL = None

DECLARATIONS = [
    {
        "name": "bewaak",
        "description": (
            "Hou iets in de gaten en meld het Wouter zodra het gebeurt (hardop als hij thuis is en jij "
            "wakker bent, anders een melding op zijn telefoon). Soorten: 'print_klaar' (de lopende "
            "3D-print is klaar of mislukt), 'persoon' (iemand bij een buitencamera), 'lucht' (een "
            "luchtwaarde gaat over of onder een grens, bijv. co2_ppm boven 1000), 'camera' (kijk elke "
            "paar minuten met je eigen camera en meld als het antwoord op een ja/nee-vraag 'ja' is), "
            "'timer' (na een aantal minuten). Gebruik dit ook voor 'laat het me weten als...' en "
            "'hou de voordeur in de gaten tot tien uur'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "soort": {"type": "string", "enum": ["print_klaar", "persoon", "lucht", "camera", "timer"]},
                "omschrijving": {"type": "string", "description": "Korte titel voor de app, bijv. 'Epoxy uitgehard'."},
                "tot": {"type": "string", "description": "Tot hoe laat, 'HH:MM' (vandaag, of morgen als het al later is). Leeg = standaard."},
                "minuten": {"type": "integer", "description": "timer: na hoeveel minuten. camera: om de hoeveel minuten kijken (min 5)."},
                "camera": {"type": "string", "description": "persoon: Voordeur, Garage of Achtertuin; leeg = alle."},
                "waarde": {"type": "string", "description": "lucht: co2_ppm, pm2_5, voc_index, temperatuur, vocht (of de naam zoals Brandstof hem noemt)."},
                "grens": {"type": "number", "description": "lucht: de grenswaarde."},
                "richting": {"type": "string", "enum": ["boven", "onder"], "description": "lucht: melden als de waarde boven of onder de grens komt."},
                "vraag": {"type": "string", "description": "camera: een ja/nee-vraag over wat je ziet, bijv. 'zit de kat op tafel?'."},
            },
            "required": ["soort"],
        },
    },
    {
        "name": "bewaak_lijst",
        "description": "Welke dingen je nu in de gaten houdt, met hun nummer.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "bewaak_stop",
        "description": "Stop met iets in de gaten houden (nummer uit bewaak_lijst, of 'alles').",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
]
NAMES = {d["name"] for d in DECLARATIONS}


def forward(name: str, args: dict) -> dict:
    """On the robot: run the tool on the home server."""
    if HUB_CALL is None:
        return {"fout": "De thuisserver is niet verbonden, dus bewaken kan nu niet."}
    return HUB_CALL(name, args or {}, TIMEOUT_S)


HANDLERS = {n: (lambda n: lambda **kw: forward(n, kw))(n) for n in NAMES}


def card() -> dict:
    return {"draait op": "thuisserver, via MQTT"}
