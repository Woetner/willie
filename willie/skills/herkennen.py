"""Recognise and remember things, people and the cat (K2, D27): tools, run on the home server.

The memory lives only on the server (`homeserver/hub/geheugen.py`: photos, face and image
prints, where and when seen). The robot forwards the calls; the server asks the robot for a
photo itself (willie/cmd/photo), so these tools need no arguments with pictures.

Not to be confused with `onthoud` / `herinner` (voice/tools.py): those keep notes and
conversations; these recognise what the camera sees.
"""
from __future__ import annotations

LABEL = "Herkennen: things, people (with consent) and the cat (K2, on the home server)"
TIMEOUT_S = 45.0

# Set by tools/willie_voice.py when the MQTT bridge runs: hub_call(name, args, timeout) -> dict.
HUB_CALL = None

DECLARATIONS = [
    {
        "name": "onthoud_ding",
        "description": (
            "Onthoud hoe iets eruitziet dat Wouter voor je camera houdt of aanwijst ('onthoud dit, dit is mijn "
            "multimeter'), zodat je het later herkent en weet waar je het zag. Ook voor een dier (soort 'dier'). "
            "Nooit voor mensen: daarvoor is leer_persoon."
        ),
        "parameters": {"type": "object", "properties": {
            "naam": {"type": "string", "description": "Hoe het heet, bijv. 'mijn sleutels', 'de rode schroevendraaier'."},
            "soort": {"type": "string", "enum": ["ding", "dier"]},
            "feit": {"type": "string", "description": "Optioneel iets om erbij te onthouden."}},
            "required": ["naam"]},
    },
    {
        "name": "leer_persoon",
        "description": (
            "Onthoud iemands gezicht zodat je hem of haar herkent. ALLEEN met toestemming: vraag eerst 'Mag ik je "
            "onthouden? Ik bewaar een paar foto's van je gezicht op de thuisserver.' en roep dit pas bij 'ja' aan "
            "met toestemming=true. De persoon kijkt in je camera."
        ),
        "parameters": {"type": "object", "properties": {
            "naam": {"type": "string"}, "toestemming": {"type": "boolean"}, "feit": {"type": "string"}},
            "required": ["naam", "toestemming"]},
    },
    {
        "name": "wie_is_dit",
        "description": "Kijk met je camera en zeg wie of wat je herkent (mensen, de kat, dingen die je kent).",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "waar_is",
        "description": "Waar en wanneer je iets of iemand die je kent voor het laatst zag ('waar zijn mijn sleutels?').",
        "parameters": {"type": "object", "properties": {"naam": {"type": "string"}}, "required": ["naam"]},
    },
    {
        "name": "wat_weet_je_over",
        "description": "Alles wat je weet over iets of iemand die je herkent: feiten, hoe vaak gezien, sinds wanneer.",
        "parameters": {"type": "object", "properties": {"naam": {"type": "string"}}, "required": ["naam"]},
    },
    {
        "name": "onthoud_feit",
        "description": "Voeg een feit toe aan iets of iemand die je herkent ('tante Ria drinkt koffie zwart').",
        "parameters": {"type": "object", "properties": {"naam": {"type": "string"}, "feit": {"type": "string"}},
                       "required": ["naam", "feit"]},
    },
    {
        "name": "vergeet",
        "description": (
            "Wis alles over iets of iemand (foto's, afdrukken, waar gezien). Vraag eerst of hij het zeker weet, "
            "dan opnieuw met bevestigd=true. Iemand mag altijd vragen om vergeten te worden."
        ),
        "parameters": {"type": "object", "properties": {"naam": {"type": "string"}, "bevestigd": {"type": "boolean"}},
                       "required": ["naam"]},
    },
    {
        "name": "wie_ken_je",
        "description": "Welke dingen, mensen en dieren je kunt herkennen.",
        "parameters": {"type": "object", "properties": {}},
    },
]
NAMES = {d["name"] for d in DECLARATIONS}


def forward(name: str, args: dict) -> dict:
    if HUB_CALL is None:
        return {"fout": "De thuisserver is niet verbonden, dus herkennen kan nu niet."}
    return HUB_CALL(name, args or {}, TIMEOUT_S)


HANDLERS = {n: (lambda n: lambda **kw: forward(n, kw))(n) for n in NAMES}


def card() -> dict:
    return {"draait op": "thuisserver (foto's en afdrukken blijven daar)"}
