"""Garage mode's workshop tools (K1, D28): the tool list, shared by the robot and the hub.

The work happens on the home server (`homeserver/hub/garage.py`): the parts library and the
datasheet check, the vehicle notes, step plans and the shopping list. The robot forwards the
calls over MQTT and puts what comes back on its face: a pinout as a zoomable drawing, a step
plan as a card. `pinout_zoom` and `pinout_weg` only touch the face.
"""
from __future__ import annotations

LABEL = "Garage: pinouts, step plans, Kever/Tomos, shopping list (K1, on the home server)"
TIMEOUT_S = {"pinout": 150.0, "stappenplan": 60.0}      # a new pinout: search + datasheet check

# Set by tools/willie_voice.py: HUB_CALL(name, args, timeout) -> dict, FACE = the Face.
HUB_CALL = None
FACE = None

DECLARATIONS = [
    {
        "name": "pinout",
        "description": (
            "Zoek de pinout van een onderdeel (chip, module, board, sensor, transistor, connector) en zet hem "
            "als tekening op je gezicht. Uit de bibliotheek op de thuisserver is het meteen klaar; een nieuw "
            "onderdeel duurt tot een minuut (opzoeken + controleren tegen de datasheet): zeg dat eerst. "
            "Met 'pin' licht je een pin op en zoom je erop in (bijv. 'SDA', 'GPIO21', '3')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "onderdeel": {"type": "string", "description": "Zo precies mogelijk, bijv. 'ESP32 DevKit V1 30-pin', 'NE555', 'TB6612FNG module'."},
                "pin": {"type": "string", "description": "Optioneel: welke pin oplichten."},
                "opnieuw": {"type": "boolean", "description": "true = niet uit de bibliotheek, opnieuw opzoeken (als hij fout was)."},
            },
            "required": ["onderdeel"],
        },
    },
    {
        "name": "pinout_zoom",
        "description": "Licht een pin op in de pinout die al op je gezicht staat en zoom erop in ('zoom op GPIO21').",
        "parameters": {"type": "object", "properties": {"pin": {"type": "string"}}, "required": ["pin"]},
    },
    {
        "name": "pinout_weg",
        "description": "Haal de pinout van je gezicht.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "pinout_vergeet",
        "description": "Gooi een pinout uit de bibliotheek, bijvoorbeeld als Wouter zegt dat hij fout is.",
        "parameters": {"type": "object", "properties": {"onderdeel": {"type": "string"}}, "required": ["onderdeel"]},
    },
    {
        "name": "onderdelen",
        "description": "Welke onderdelen er in de pinout-bibliotheek staan (gecontroleerd of niet).",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "stappenplan",
        "description": (
            "Een stappenplan voor een klus (volgorde, aanhaalmomenten, maten), met Wouters eigen notities over "
            "de Kever of Tomos. Het plan komt op je gezicht; lees daarna één stap per keer voor."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "taak": {"type": "string", "description": "Bijv. 'cilinderkop monteren', 'klepspeling afstellen'."},
                "voertuig": {"type": "string", "description": "kever, tomos of leeg."},
            },
            "required": ["taak"],
        },
    },
    {
        "name": "voertuig",
        "description": "Wat Wouter zelf over zijn Kever of Tomos heeft vastgelegd (gegevens, eigen maten, logboek).",
        "parameters": {
            "type": "object",
            "properties": {
                "voertuig": {"type": "string", "enum": ["kever", "tomos"]},
                "onderwerp": {"type": "string", "description": "Optioneel: waar het over gaat, bijv. 'bougie'."},
            },
            "required": ["voertuig"],
        },
    },
    {
        "name": "voertuig_log",
        "description": "Schrijf een klus, meting of afstelling in het logboek van de Kever of Tomos.",
        "parameters": {
            "type": "object",
            "properties": {
                "voertuig": {"type": "string", "enum": ["kever", "tomos"]},
                "notitie": {"type": "string", "description": "Bijv. 'klepspeling ingesteld op 0,15 mm, koud'."},
            },
            "required": ["voertuig", "notitie"],
        },
    },
    {
        "name": "boodschappen",
        "description": "De boodschappenlijst voor de werkplaats (staat ook in de app): toevoegen, lijst, weg of leeg.",
        "parameters": {
            "type": "object",
            "properties": {
                "actie": {"type": "string", "enum": ["toevoegen", "lijst", "weg", "leeg"]},
                "item": {"type": "string", "description": "Bijv. 'BC547 transistor'."},
                "aantal": {"type": "string"},
                "winkel": {"type": "string", "description": "Optioneel, bijv. 'Conrad', 'AliExpress', 'Kever-onderdelen'."},
            },
            "required": ["actie"],
        },
    },
]
NAMES = {d["name"] for d in DECLARATIONS}
FACE_ONLY = {"pinout_zoom", "pinout_weg"}
SERVER_NAMES = NAMES - FACE_ONLY           # what the hub runs itself (phone chat, call)


def forward(name: str, args: dict) -> dict:
    """On the robot: run the tool on the home server."""
    if HUB_CALL is None:
        return {"fout": "De thuisserver is niet verbonden, dus dit kan nu niet."}
    return HUB_CALL(name, args or {}, TIMEOUT_S.get(name, 20.0))


def _pin_lines(data: dict) -> list[str]:
    lines = []
    for p in data.get("pinnen") or []:
        text = f"{p.get('nr')} {p.get('naam')}"
        if p.get("functie"):
            text += f" ({p['functie']})"
        if p.get("gevaar"):
            text += f" LET OP: {p['gevaar']}"
        lines.append(text)
    return lines


def pinout(onderdeel: str, pin: str = "", opnieuw: bool = False) -> dict:
    data = forward("pinout", {"onderdeel": onderdeel, "opnieuw": bool(opnieuw)})
    if "fout" in data:
        return data
    shown = {}
    if FACE is not None:
        shown = FACE.show_pinout(data, pin=pin)
    answer = {
        "naam": data.get("naam"),
        "gecontroleerd": {2: "ja, tegen de datasheet", 1: "alleen de functies per pin (niet de volgorde)"}.get(
            data.get("controle_niveau", 2 if data.get("gecontroleerd") else 0), "NEE, niet gecontroleerd"),
        "controle": data.get("controle") or "",
        "voeding": data.get("voeding", ""),
        "gevaren": data.get("gevaren") or [],
        "pinnen": _pin_lines(data),
        "op_scherm": bool(shown and "fout" not in shown),
    }
    if shown.get("pin"):
        answer["opgelicht"] = shown["pin"]
    return answer


def pinout_zoom(pin: str) -> dict:
    if FACE is None:
        return {"fout": "geen scherm"}
    return FACE.pinout_focus(pin)


def pinout_weg() -> dict:
    if FACE is not None:
        FACE.dismiss()
    return {"ok": True}


def stappenplan(taak: str, voertuig: str = "") -> dict:
    plan = forward("stappenplan", {"taak": taak, "voertuig": voertuig})
    if "fout" in plan or FACE is None:
        return plan
    lines = [plan.get("titel", taak).upper()]
    for step in plan.get("stappen") or []:
        value = f" [{step['waarde']}]" if step.get("waarde") else ""
        lines.append(f"{step.get('nr')}. {step.get('tekst', '')}{value}")
    FACE.show("\n".join(lines))
    return plan


HANDLERS = {n: (lambda n: lambda **kw: forward(n, kw))(n) for n in NAMES}
HANDLERS.update({"pinout": pinout, "pinout_zoom": pinout_zoom, "pinout_weg": pinout_weg,
                 "stappenplan": stappenplan})


def card() -> dict:
    return {"draait op": "thuisserver (bibliotheek, datasheets) + gezicht"}
