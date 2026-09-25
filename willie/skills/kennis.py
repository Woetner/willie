"""L5 knowledge base (25 Sep): WILL-E's own library on the home server
(`homeserver/hub/kennis.py`): the docs folder, the parts dossier with its datasheets, the
Kever/Tomos notes and the pinout library, searched by meaning (multilingual-e5 on the
server's vision worker). The robot only forwards the question. Server gone = one sentence (D22).
"""
from __future__ import annotations

LABEL = "Kennisbank - own documents, datasheets, vehicle notes (L5, home server)"

TIMEOUT_S = 8.0

# Set by tools/willie_voice.py when the MQTT bridge runs: hub_call(name, args, timeout) -> dict.
HUB_CALL = None

DECLARATIONS = [
    {
        "name": "zoek_kennis",
        "description": (
            "Zoek in je eigen bibliotheek op de thuisserver: datasheets en README's van je onderdelen "
            "(LM2596, TB6612, INA219, VL53L0X, INMP441...), Wouters documenten, de Kever/Tomos-notities "
            "en de pinouts. Gebruik dit vóór internet bij vragen over een onderdeel, een maat, een "
            "pin of iets wat Wouter heeft opgeschreven. Je krijgt de beste stukken tekst met de bron."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "vraag": {"type": "string", "description": "De vraag, volledig, bijvoorbeeld 'maximale ingangsspanning LM2596'."},
                "bron": {"type": "string", "description": "Optioneel: onderdelen, docs, voertuig of pinout."},
            },
            "required": ["vraag"],
        },
    },
]
NAMES = {d["name"] for d in DECLARATIONS}


def zoek_kennis(vraag: str, bron: str = "") -> dict:
    if HUB_CALL is None:
        return {"fout": "De thuisserver is niet verbonden, dus in mijn bibliotheek kan ik nu niet kijken."}
    return HUB_CALL("zoek_kennis", {"vraag": vraag, "bron": bron}, TIMEOUT_S)


HANDLERS = {"zoek_kennis": zoek_kennis}


def card() -> dict:
    return {"draait op": "thuisserver, via MQTT", "zoekt": "op betekenis (e5)"}
