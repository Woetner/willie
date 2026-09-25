"""L3 house-now snapshot (25 Sep): one short line about home, built on the home server
(`homeserver/hub/situation.py`): presence, agenda, next reminder, weather, printer, air, last
person at a door, his own state. Goes into the context at session start and is the tool
`huis_nu`. Server gone = he says so in one sentence (D22).
"""
from __future__ import annotations

LABEL = "Huis nu - one-line situation from the home server (L3)"

TIMEOUT_S = 4.0
CONTEXT_TIMEOUT_S = 1.5   # at session start: a slow server must not hold up the first word

# Set by tools/willie_voice.py when the MQTT bridge runs: hub_call(name, args, timeout) -> dict.
HUB_CALL = None

DECLARATIONS = [
    {
        "name": "huis_nu",
        "description": (
            "Hoe het er nu thuis voor staat, vers van de thuisserver: of Wouter thuis is, wat er nog "
            "in de agenda staat, de volgende herinnering, het weer, de printer, de lucht, wie er bij "
            "de deur was en je eigen toestand. 'Thuis nu' in je context is van het begin van dit "
            "gesprek: antwoord daaruit. Roep dit alleen aan als het gesprek al een tijd loopt of "
            "als daar niet in staat wat je zoekt, voordat je zegt dat je het niet weet."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
]
NAMES = {d["name"] for d in DECLARATIONS}


def now(timeout: float = TIMEOUT_S) -> dict:
    if HUB_CALL is None:
        return {"fout": "De thuisserver is niet verbonden, dus hoe het thuis staat weet ik nu niet."}
    return HUB_CALL("huis_nu", {}, timeout)


def context_line() -> str:
    """For live_context(): the line, or "" when the server is slow or gone."""
    if HUB_CALL is None:
        return ""
    result = now(CONTEXT_TIMEOUT_S)
    return str(result.get("nu") or "") if isinstance(result, dict) else ""


HANDLERS = {"huis_nu": lambda **_: now()}


def card() -> dict:
    return {"draait op": "thuisserver, via MQTT", "ververst": "elke minuut"}
