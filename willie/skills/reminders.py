"""Reminders + agenda (H4, R1-R7): the tool list, shared by the robot and the hub.

The reminders live on the home server (`homeserver/hub/reminders/`, SQLite), because they
need the calendars, the weather, the phone's push and a clock that runs while the Pi is
asleep. This file is the single source of the declarations: the hub imports them from
willie-lib, and the robot's voice session forwards the calls to the hub over MQTT
(`willie/hub/call` -> `willie/hub/result`, see remote.py). With the server off, the robot
says so in one sentence (D22).
"""
from __future__ import annotations

LABEL = "Herinneringen + agenda (H4, on the home server)"
CATEGORIES = ["school", "project", "kever", "gezondheid", "sociaal", "formeel", "thuis", "overig"]
TIMEOUT_S = 20.0

# Set by tools/willie_voice.py when the MQTT bridge runs: hub_call(name, args, timeout) -> dict.
HUB_CALL = None

DECLARATIONS = [
    {
        "name": "herinnering_maak",
        "description": (
            "Maak een herinnering of taak voor Wouter. Gebruik dit zodra hij zegt dat hij iets "
            "niet mag vergeten, iets moet doen of voorbereiden, of 'herinner me'. De thuisserver "
            "kiest zelf slimme momenten (met zijn agenda, rooster en werk) en herinnert hem via "
            "de app of hardop. Geef een deadline als er een is, een tijdstip alleen als hij een "
            "exact moment noemt."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "wat": {"type": "string", "description": "Wat hij moet doen, kort en concreet, bijv. 'Teams-meeting projectgroep voorbereiden'."},
                "deadline": {"type": "string", "description": "Wanneer het af of gebeurd moet zijn, als 'YYYY-MM-DD HH:MM' of 'YYYY-MM-DD'. Leeg als er geen is."},
                "tijdstip": {"type": "string", "description": "Alleen als hij een exact herinnermoment noemt ('morgen om 9 uur'), als 'YYYY-MM-DD HH:MM'."},
                "categorie": {"type": "string", "enum": CATEGORIES},
                "prioriteit": {"type": "string", "enum": ["hoog", "normaal", "laag"],
                               "description": "hoog = mag echt niet misgaan (school, anderen afhankelijk, geld). laag = alleen in de briefing."},
                "mensen": {"type": "string", "description": "Wie er bij betrokken zijn, bijv. 'projectgroep' of 'Jan'."},
                "details": {"type": "string", "description": "Alles wat hij er verder over zei."},
            },
            "required": ["wat"],
        },
    },
    {
        "name": "herinneringen",
        "description": "Lees Wouters herinneringen en taken, met hun id's en de geplande herinnermomenten.",
        "parameters": {
            "type": "object",
            "properties": {
                "filter": {"type": "string", "enum": ["open", "vandaag", "week", "klaar"], "description": "Standaard open."},
                "categorie": {"type": "string", "enum": CATEGORIES},
            },
        },
    },
    {
        "name": "herinnering_wijzig",
        "description": (
            "Werk een herinnering bij. actie: info = extra informatie toevoegen (hij plant opnieuw), "
            "klaar = gedaan, schrap = laten vallen, snooze = later opnieuw, gezien = hij heeft de "
            "herinnering gehoord (stopt het herhalen), deadline = nieuwe deadline. Zoek het id eerst "
            "op met herinneringen als je het niet weet."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "actie": {"type": "string", "enum": ["info", "klaar", "schrap", "snooze", "gezien", "deadline"]},
                "tekst": {"type": "string", "description": "Bij info: de nieuwe informatie."},
                "deadline": {"type": "string", "description": "Bij deadline: 'YYYY-MM-DD HH:MM'."},
                "minuten": {"type": "integer", "description": "Bij snooze, standaard 60."},
            },
            "required": ["id", "actie"],
        },
    },
    {
        "name": "werkrooster",
        "description": (
            "Wouters werkrooster (vast: di 15:30-22:00, do 15:30-22:00, za 08:30-20:00). Pas het "
            "meteen aan als hij zegt dat hij extra, minder of anders werkt. actie toon = de "
            "komende diensten."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "actie": {"type": "string", "enum": ["toon", "extra", "vrij", "anders", "normaal"],
                          "description": "extra = extra dienst, vrij = hij werkt die dag niet, anders = andere tijden, normaal = terug naar het vaste rooster."},
                "datum": {"type": "string", "description": "YYYY-MM-DD"},
                "start": {"type": "string", "description": "HH:MM"},
                "eind": {"type": "string", "description": "HH:MM"},
            },
            "required": ["actie"],
        },
    },
    {
        "name": "agenda",
        "description": "Lees Wouters agenda: afspraken, lesrooster, Canvas-deadlines en werkdiensten.",
        "parameters": {
            "type": "object",
            "properties": {
                "datum": {"type": "string", "description": "'vandaag', 'morgen' of YYYY-MM-DD. Standaard vandaag."},
                "dagen": {"type": "integer", "description": "Hoeveel dagen vanaf die datum, standaard 1, max 14."},
            },
        },
    },
]
NAMES = {d["name"] for d in DECLARATIONS}


def forward(name: str, args: dict) -> dict:
    """On the robot: run the tool on the home server."""
    if HUB_CALL is None:
        return {"fout": "De thuisserver is niet verbonden, dus herinneringen kan ik nu niet bijwerken."}
    return HUB_CALL(name, args or {}, TIMEOUT_S)


HANDLERS = {n: (lambda n: lambda **kw: forward(n, kw))(n) for n in NAMES}


def card() -> dict:
    # The MQTT link lives in the voice process, not in the dashboard: say where it runs.
    return {"draait op": "thuisserver, via MQTT"}
