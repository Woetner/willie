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
        "name": "verbeter_jezelf",
        "description": (
            "Voer een verbetering aan je eigen code door. Roep dit ALLEEN aan nadat je "
            "hardop hebt verteld wat je gaat doen en Wouter ja heeft gezegd. Bij risico "
            "'hoog' vraag je het een tweede keer voordat je dit aanroept. Claude Code op "
            "zijn laptop schrijft de code, test hem en zet hem daarna op jou."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "opdracht": {
                    "type": "string",
                    "description": (
                        "Wat er moet veranderen, concreet en op zichzelf te begrijpen door "
                        "iemand die dit gesprek niet gehoord heeft. Noem het bestand of de "
                        "functie als je die weet."
                    ),
                },
                "plan": {
                    "type": "string",
                    "description": "Wat je Wouter net hardop hebt verteld dat je gaat doen.",
                },
                "risico": {
                    "type": "string",
                    "enum": ["laag", "hoog"],
                    "description": (
                        "'hoog' bij alles wat je kapot kan maken: opstarten, systemd, "
                        "config.txt, audio- of scherminstellingen, netwerk. Anders 'laag'."
                    ),
                },
                "bevestigd": {
                    "type": "boolean",
                    "description": "True alleen als Wouter hardop ja heeft gezegd. Bij 'hoog': twee keer.",
                },
            },
            "required": ["opdracht", "plan", "risico", "bevestigd"],
        },
    },
    {
        "name": "verbeteringen_status",
        "description": (
            "Kijk hoe het met je eigen verbeteringen staat: wat er loopt, wat er klaar is, "
            "wat er mislukt is. Gebruik dit als Wouter vraagt of het al gelukt is."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "lees_code",
        "description": (
            "Lees een bestand uit je eigen code. Gebruik dit voordat je iets zegt over hoe "
            "je werkt, en altijd voordat je een verbetering voorstelt: kijk eerst wat er nu "
            "staat. Wouter hoeft je geen code voor te lezen."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pad": {
                    "type": "string",
                    "description": "Pad binnen je repository, bijvoorbeeld willie/audio/speech.py of config/persona.md.",
                },
                "vanaf_regel": {"type": "integer", "description": "Optioneel: begin hier (1 is het begin)."},
            },
            "required": ["pad"],
        },
    },
    {
        "name": "zoek_in_code",
        "description": (
            "Zoek een woord of stukje tekst in je eigen code en krijg de bestanden en "
            "regelnummers terug. Gebruik dit als je niet weet waar iets staat."
        ),
        "parameters": {
            "type": "object",
            "properties": {"term": {"type": "string", "description": "Waar je op zoekt."}},
            "required": ["term"],
        },
    },
    {
        "name": "lijst_code",
        "description": "Kijk welke bestanden er in een map van je code staan.",
        "parameters": {
            "type": "object",
            "properties": {"map": {"type": "string", "description": "Bijvoorbeeld willie/voice of tools. Leeg = de hoofdmap."}},
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


# Called with the photo's path right after kijk() captures it, before it goes to the model,
# so the face can show what he is looking at (set by the live session when a face exists).
LOOK_HOOK = None


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
            if LOOK_HOOK:
                try:
                    LOOK_HOOK(image)
                except Exception:          # showing the photo is a nicety; the answer matters
                    pass
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


QUEUE_FILE = Path(os.environ.get("WILLIE_IMPROVE_QUEUE", REPO / ".local" / "improve_queue.jsonl"))


RESULT_FILE = Path(os.environ.get("WILLIE_IMPROVE_RESULTS", REPO / ".local" / "improve_results.jsonl"))


def verbeter_jezelf(opdracht: str, plan: str = "", risico: str = "laag", bevestigd: bool = False) -> dict:
    """Queue an approved code change for the worker on the Mac.

    The robot still cannot edit itself: this appends one line to a file. What
    changed from the first version is that the approval is spoken, so `bevestigd`
    is the model's word for "he said yes". That is a soft gate, not a hard one -
    the hard gates are on the worker side: a branch, tests, a health check and
    an automatic rollback.
    """
    opdracht = " ".join(opdracht.split())
    if len(opdracht) < 10:
        return {"fout": "te vaag, zeg concreter wat er moet veranderen"}
    if not bevestigd:
        return {"fout": "niet bevestigd - vertel eerst wat je gaat doen en vraag of het mag"}
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime

    entry = {
        "gevraagd": datetime.now().isoformat(timespec="seconds"),
        "opdracht": opdracht,
        "plan": " ".join(plan.split()),
        "risico": "hoog" if risico == "hoog" else "laag",
    }
    with QUEUE_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"ok": True, "opgepakt": opdracht, "opmerking": "duurt een paar minuten"}


def verbeteringen_status() -> dict:
    wachtrij = 0
    if QUEUE_FILE.exists():
        wachtrij = sum(1 for line in QUEUE_FILE.read_text(encoding="utf-8").splitlines() if line.strip())
    klaar = []
    if RESULT_FILE.exists():
        for line in RESULT_FILE.read_text(encoding="utf-8").splitlines()[-3:]:
            try:
                klaar.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return {"in_wachtrij": wachtrij, "laatste": klaar or "nog niets afgerond"}


# Read-only, and never these: the API keys live in .env, and the wake-word and
# model files are third-party binaries that say nothing useful out loud.
FORBIDDEN = (".env", "config/wakewords", ".local", ".git")
READABLE_SUFFIXES = (".py", ".md", ".sh", ".yaml", ".yml", ".txt", ".toml", ".cfg", ".ini", ".service", "Makefile")
MAX_CHARS = 3000        # about a minute of speech; more is useless in a conversation


def _safe_path(pad: str) -> Path | None:
    """Resolve inside the repository, or return None. Blocks ../ and secrets."""
    try:
        target = (REPO / pad.strip().lstrip("/")).resolve()
    except (OSError, RuntimeError):
        return None
    if not str(target).startswith(str(REPO.resolve())):
        return None
    relative = str(target.relative_to(REPO.resolve()))
    if any(relative == f or relative.startswith(f + "/") for f in FORBIDDEN):
        return None
    return target


def lees_code(pad: str, vanaf_regel: int = 1) -> dict:
    target = _safe_path(pad)
    if target is None:
        return {"fout": "dat pad mag ik niet lezen"}
    if not target.is_file():
        return {"fout": f"{pad} bestaat niet"}
    if target.suffix and target.suffix not in READABLE_SUFFIXES and target.name != "Makefile":
        return {"fout": f"{target.suffix} is geen tekstbestand"}
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return {"fout": str(exc)}
    start = max(1, int(vanaf_regel or 1))
    body = "\n".join(f"{n}: {line}" for n, line in enumerate(lines[start - 1:], start))
    truncated = len(body) > MAX_CHARS
    return {
        "pad": pad,
        "regels_totaal": len(lines),
        "inhoud": body[:MAX_CHARS],
        "afgekapt": truncated,
    }


def zoek_in_code(term: str) -> dict:
    term = term.strip()
    if len(term) < 2:
        return {"fout": "te kort om op te zoeken"}
    try:
        found = subprocess.run(
            ["grep", "-rn", "--include=*.py", "--include=*.md", "--include=*.sh", "--include=*.yaml",
             "--exclude-dir=.git", "--exclude-dir=.local", "--exclude-dir=.venv", "--exclude-dir=wakewords",
             "-i", term, "."],
            cwd=REPO, capture_output=True, text=True, timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return {"fout": str(exc)}
    # Filter on the file the hit is in, not on the text: a plain ".env" substring
    # test also throws away every line that mentions os.environ.
    hits = []
    for line in found.splitlines():
        path = line.split(":", 1)[0].lstrip("./")
        if any(path == f or path.startswith(f + "/") for f in FORBIDDEN):
            continue
        hits.append(line[:160])
        if len(hits) >= 20:
            break
    return {"treffers": hits or "niets gevonden", "aantal": len(hits)}


def lijst_code(map: str = "") -> dict:
    target = _safe_path(map or ".")
    if target is None or not target.is_dir():
        return {"fout": "die map mag ik niet bekijken"}
    entries = []
    for item in sorted(target.iterdir()):
        if item.name.startswith(".") or item.name in ("__pycache__", "wakewords"):
            continue
        entries.append(item.name + ("/" if item.is_dir() else ""))
    return {"map": map or ".", "bestanden": entries[:40]}


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


from willie.skills import brandstof  # noqa: E402  (Wouter's health monitor on the Mac)

DECLARATIONS.append(brandstof.DECLARATION)

HANDLERS = {
    "gezondheid": brandstof.gezondheid,
    "kijk": kijk,
    "lees_code": lees_code,
    "zoek_in_code": zoek_in_code,
    "lijst_code": lijst_code,
    "onthoud": onthoud,
    "status": status,
    "zet_volume": zet_volume,
    "verbeter_jezelf": verbeter_jezelf,
    "verbeteringen_status": verbeteringen_status,
}


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
