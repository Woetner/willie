"""Search, adventure and sentry by voice (K3-K5): start, stop, ask how it goes.

The missions themselves live in willie/missions/ (a thread in this voice process that
drives through the core's control socket); this file is only the tool list.
"""
from __future__ import annotations

LABEL = "Missies: zoeken, avontuur, wachtmodus (K3-K5)"

DECLARATIONS = [
    {
        "name": "zoek",
        "description": (
            "Ga iets, iemand of een dier zoeken in huis: eerst in je geheugen (waar laatst gezien), dan rijd je "
            "rond en kijk je om je heen, daarna de buitencamera's. Loopt op de achtergrond; je meldt het als je "
            "het vindt. Geef bij iets dat je nog niet kent een Engelse omschrijving mee."
        ),
        "parameters": {"type": "object", "properties": {
            "naam": {"type": "string", "description": "Zoals je het kent, bijv. 'de kat', 'mijn sleutels', 'mama'."},
            "omschrijving_en": {"type": "string", "description": "Engels, voor wat je niet kent: 'a red screwdriver'."}}},
    },
    {
        "name": "avontuur",
        "description": (
            "Ga op avontuur: rij het huis rond, bekijk alles, maak een kaart van kamers en spullen en vertel "
            "daarna wat je ontdekte. Duurt tot een half uur. Alleen als Wouter erom vraagt."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "wachtmodus",
        "description": (
            "Bewaak het huis (wachtmodus aan of uit): je let op harde geluiden, stoten, veranderingen in beeld en "
            "mensen bij de buitencamera's, kijkt dan rond en stuurt foto's naar Wouters telefoon. Alleen aan of "
            "uit als Wouter het vraagt."
        ),
        "parameters": {"type": "object", "properties": {"aan": {"type": "boolean"}}, "required": ["aan"]},
    },
    {
        "name": "missie_stop",
        "description": "Stop wat je nu aan het doen bent (zoeken, avontuur of wachtmodus).",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "missie_status",
        "description": "Hoe het gaat met het zoeken, het avontuur of de wachtmodus.",
        "parameters": {"type": "object", "properties": {}},
    },
]
NAMES = {d["name"] for d in DECLARATIONS}


def zoek(naam: str = "", omschrijving_en: str = "") -> dict:
    from willie import missions
    return missions.start("zoek", naam=naam, omschrijving_en=omschrijving_en)


def avontuur() -> dict:
    from willie import missions
    return missions.start("avontuur")


def wachtmodus(aan: bool) -> dict:
    from willie import missions
    if aan:
        return missions.start("wacht")
    status = missions.status()
    return missions.stop() if status.get("missie") == "wacht" else {"ok": True, "was": "niet aan"}


def missie_stop() -> dict:
    from willie import missions
    return missions.stop()


def missie_status() -> dict:
    from willie import missions
    return missions.status()


HANDLERS = {"zoek": zoek, "avontuur": avontuur, "wachtmodus": wachtmodus, "missie_stop": missie_stop,
            "missie_status": missie_status}


def card() -> dict:
    from willie import missions
    status = missions.status()
    return {"nu": status.get("missie") or "niets", "voortgang": status.get("voortgang") or ""}
