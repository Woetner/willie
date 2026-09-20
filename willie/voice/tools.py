"""What WILL-E can actually do during a conversation (Live API function calls).

Deliberately a short, fixed list. The model never gets a shell: every tool here
is a named action with checked arguments, so the worst a confused or
manipulated model can do is take a photo, write a note, or read a number that
was already on the dashboard.

Adding a tool is adding a function plus its declaration. Anything that spends
money, messages a person, or moves the robot needs spoken confirmation first
(persona "Grenzen", §3.1 boundaries) and does not belong here yet.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MEMORY_FILE = Path(os.environ.get("WILLIE_MEMORY", REPO / "config" / "memory.md"))

DECLARATIONS = [
    {
        "name": "kijk",
        "description": (
            "Kijk door de camera en beschrijf wat je ziet. Gebruik dit zodra Wouter iets "
            "laat zien, vraagt wat je ziet, of iets wil laten herkennen (onderdeel, "
            "printplaat, kabel, print)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "waar_op_letten": {
                    "type": "string",
                    "description": "Waar je specifiek op moet letten, bijvoorbeeld 'welke pinnen zijn gesoldeerd'.",
                }
            },
        },
    },
    {
        "name": "onthoud",
        "description": (
            "Sla iets op dat je later moet weten: een voorkeur van Wouter, een maat, een "
            "instelling, een afspraak. Gebruik dit uit jezelf zodra hij iets zegt dat "
            "morgen nog waar is."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "notitie": {"type": "string", "description": "Eén korte zin, concreet geformuleerd."},
            },
            "required": ["notitie"],
        },
    },
    {
        "name": "status",
        "description": "Lees de toestand van de Pi: temperatuur, vrij geheugen, voeding, uptime.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "zet_volume",
        "description": "Zet je eigen spreekvolume. 0.05 is fluisteren, 0.15 is normaal, 0.5 is hard.",
        "parameters": {
            "type": "object",
            "properties": {"niveau": {"type": "number", "description": "Tussen 0.02 en 0.8."}},
            "required": ["niveau"],
        },
    },
]


def kijk(waar_op_letten: str = "") -> dict:
    import sys

    sys.path.insert(0, str(REPO / "tools"))
    from ask_camera import ask_gemini, capture

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return {"fout": "geen API-sleutel"}
    question = waar_op_letten or "Wat zie je? Beschrijf het kort en concreet."
    try:
        with tempfile.TemporaryDirectory(prefix="willie-look-") as tmp:
            image = Path(tmp) / "view.jpg"
            capture(image, quiet=True)
            return {"gezien": ask_gemini(image, question, key)}
    except RuntimeError as exc:
        return {"fout": str(exc)}


def onthoud(notitie: str) -> dict:
    notitie = " ".join(notitie.split())
    if not notitie:
        return {"fout": "lege notitie"}
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not MEMORY_FILE.exists():
        MEMORY_FILE.write_text("# Wat WILL-E onthoudt\n\n", encoding="utf-8")
    existing = MEMORY_FILE.read_text(encoding="utf-8")
    if notitie.lower() in existing.lower():
        return {"ok": True, "opmerking": "wist ik al"}
    MEMORY_FILE.write_text(f"{existing.rstrip()}\n- {date.today().isoformat()}: {notitie}\n", encoding="utf-8")
    return {"ok": True}


def status() -> dict:
    def shell(command: str) -> str:
        try:
            return subprocess.run(command, shell=True, capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return "?"

    free = shell("free -m | sed -n 2p").split()
    vcgencmd = shutil.which("vcgencmd") or "vcgencmd"
    return {
        "temperatuur": shell(f"{vcgencmd} measure_temp").replace("temp=", "") or "?",
        "vrij_geheugen_mb": int(free[6]) if len(free) > 6 else None,
        "voeding": "ok" if shell(f"{vcgencmd} get_throttled") == "throttled=0x0" else shell(f"{vcgencmd} get_throttled"),
        "uptime": shell("uptime -p"),
    }


def zet_volume(niveau: float) -> dict:
    from willie.audio import speech

    niveau = max(0.02, min(0.8, float(niveau)))
    speech.VOLUME = niveau
    os.environ["WILLIE_VOLUME"] = str(niveau)
    return {"ok": True, "volume": niveau}


HANDLERS = {"kijk": kijk, "onthoud": onthoud, "status": status, "zet_volume": zet_volume}


def call(name: str, arguments: dict) -> dict:
    handler = HANDLERS.get(name)
    if not handler:
        return {"fout": f"onbekende tool {name}"}
    try:
        return handler(**(arguments or {}))
    except TypeError as exc:
        return {"fout": f"verkeerde argumenten: {exc}"}
    except Exception as exc:  # a tool must never take the session down
        return {"fout": f"{type(exc).__name__}: {exc}"}


def remembered() -> str:
    """The memory file, to paste into the system prompt at session start (D10)."""
    if not MEMORY_FILE.exists():
        return ""
    notes = MEMORY_FILE.read_text(encoding="utf-8").strip()
    return "" if len(notes) < 25 else f"\n\nWat je eerder hebt onthouden:\n{notes}"
