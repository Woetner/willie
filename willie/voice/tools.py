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
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from willie.brain import memory

REPO = Path(__file__).resolve().parents[2]

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
        "name": "herinner",
        "description": (
            "Zoek in je eigen geheugen: alles wat ooit in een gesprek met Wouter is gezegd, "
            "plus je notities. Gebruik dit als hij naar een eerder gesprek vraagt ('wat zei ik "
            "gisteren over...', 'weet je nog...') of als je iets van vroeger niet meer precies weet."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "zoekterm": {"type": "string", "description": "Een paar kernwoorden, bijvoorbeeld 'Tomos carburateur'."},
            },
            "required": ["zoekterm"],
        },
    },
    {
        "name": "verbeter_jezelf",
        "description": (
            "Vraag een verbetering aan je eigen code aan. Vertel eerst hardop wat je gaat doen. "
            "Na het aanroepen moet Wouter het goedkeuren in de app op zijn telefoon; zeg dat "
            "erbij. Pas na zijn goedkeuring schrijft Claude Code op zijn laptop de code, test "
            "hem en zet hem op jou."
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
            },
            "required": ["opdracht", "plan", "risico"],
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
                    "description": (
                        "Pad binnen je repository, bijvoorbeeld willie/audio/speech.py of config/persona.md. "
                        "Het masterplan van het project is WILL-E.md."
                    ),
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
        "name": "lees_plan",
        "description": (
            "Lees het masterplan WILL-E.md: de huidige stap, een stap (A7, B12, D2), een "
            "beslissing (D18), een paragraaf (5.2, §9.4) of een onderwerp (RAM, voeding). "
            "Gebruik dit altijd als het over het plan, de volgende stap, een beslissing of "
            "een meting gaat, voordat je antwoordt."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "onderdeel": {
                    "type": "string",
                    "description": "Leeg = de huidige stap en per fase de eerstvolgende. Anders een stap-ID, paragraafnummer of zoekwoord.",
                }
            },
        },
    },
    {
        "name": "lees_logs",
        "description": (
            "Lees je eigen logboek van de Pi (journalctl): wat je hoorde, deed en welke fouten "
            "er waren. Gebruik dit als Wouter vraagt wat er misging of wat je net deed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "dienst": {
                    "type": "string",
                    "enum": ["willie-voice", "willie"],
                    "description": "willie-voice = het praten en de wake word, willie = de kern.",
                },
                "zoek": {"type": "string", "description": "Optioneel: alleen regels met dit woord, bijvoorbeeld 'error'."},
            },
        },
    },
    {
        "name": "vraag_claude",
        "description": (
            "Stel een lastige vraag aan Claude Code op Wouters laptop. Claude ziet alles van het "
            "project: het hele plan, de code, de CAD, de foto's van onderdelen, en kan op internet "
            "zoeken en datasheets lezen. Alleen lezen, hij verandert niets. Gebruik dit voor vragen "
            "over hoe iets gebouwd of aangepakt moet worden, waar jouw eigen kennis of het plan "
            "tekortschiet. Duurt 1 tot 5 minuten; het antwoord haal je op met antwoord_claude."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "vraag": {
                    "type": "string",
                    "description": "De vraag, volledig en op zichzelf te begrijpen voor iemand die dit gesprek niet hoorde.",
                }
            },
            "required": ["vraag"],
        },
    },
    {
        "name": "antwoord_claude",
        "description": "Haal de antwoorden op van vragen die je aan Claude hebt gesteld, en hoeveel er nog lopen.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "status",
        "description": "Lees de toestand van de Pi: temperatuur, vrij geheugen, voeding, uptime.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "zet_uit",
        "description": (
            "Zet jezelf helemaal uit (actie 'uit') of herstart jezelf (actie 'herstart'). "
            "Zeg eerst: 'Houd mijn scherm vast om te bevestigen.' De tool toont dan de vraag "
            "op je gezicht en wacht tot Wouter zijn vinger op het scherm houdt (ja) of kort "
            "tikt (nee). Na 'uit' kun je alleen met je aan/uit-schakelaar weer aan."
        ),
        "parameters": {
            "type": "object",
            "properties": {"actie": {"type": "string", "enum": ["uit", "herstart"]}},
            "required": ["actie"],
        },
    },
    {
        "name": "garagemodus",
        "description": (
            "Zet de garagemodus aan of uit. Aan: je luistert zonder 'Hey Willie' (alleen naar Wouters stem), "
            "je bent zijn werkplaatsmaatje (pinouts, onderdelen, stappenplannen, gevaren) en je rijdt niet, "
            "want je staat op de werkbank. Alleen als Wouter erom vraagt."
        ),
        "parameters": {"type": "object", "properties": {"aan": {"type": "boolean"}}, "required": ["aan"]},
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
# While the phone's live view holds the camera (S8), kijk() looks at its newest frame
# instead of fighting rpicam for the sensor. Set by willie/remote.py.
FRAME_SOURCE = None
# Touch confirmation on the face (security audit, 24 Sep): fn(question, timeout) -> True/False/None.
# Set by tools/willie_voice.py when there is a face with a touch screen.
CONFIRM = None
# A tool asks the live session to end once his answer has played (spotify: "speel X"
# starts the music the moment he is done talking, not after the follow-up window).
WRAP_UP = threading.Event()


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
            frame = FRAME_SOURCE() if FRAME_SOURCE else None
            if frame:
                image.write_bytes(frame)
            else:
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
    return memory.note(notitie)


def herinner(zoekterm: str) -> dict:
    return memory.search(zoekterm)


QUEUE_FILE = Path(os.environ.get("WILLIE_IMPROVE_QUEUE", REPO / ".local" / "improve_queue.jsonl"))


RESULT_FILE = Path(os.environ.get("WILLIE_IMPROVE_RESULTS", REPO / ".local" / "improve_results.jsonl"))


# Approved in the app (security audit, 24 Sep, Wouter). The model's "he said yes" is not
# enough: anyone in the room, the radio, a text held up to the camera or a web page can make
# a model say it. A request waits on the Pi (REQUEST_FILE) until Wouter taps Approve in the
# phone app; the hub sends the decision back (willie/improve/decision), and only then does it
# join QUEUE_FILE, which the Mac worker picks up. The worker checks with the hub once more.
REQUEST_FILE = Path(os.environ.get("WILLIE_IMPROVE_REQUESTS", REPO / ".local" / "improve_requests.jsonl"))
REQUEST_TTL_S = 24 * 3600
APPROVAL_HOOK = None           # fn(entry): show it in the app; set by willie/remote.py
_requests_lock = threading.Lock()


def request_hash(opdracht: str, plan: str, risico: str) -> str:
    """What Wouter approves, as one fingerprint: the Pi, the hub and the Mac worker all compute it."""
    import hashlib
    return hashlib.sha256(json.dumps([opdracht, plan, risico], ensure_ascii=False).encode()).hexdigest()


def verbeter_jezelf(opdracht: str, plan: str = "", risico: str = "laag", **_ignored) -> dict:
    """Ask for a code change. It waits for Wouter's approval in the app; nothing runs before."""
    import secrets
    from datetime import datetime

    opdracht = " ".join(opdracht.split())
    if len(opdracht) < 10:
        return {"fout": "te vaag, zeg concreter wat er moet veranderen"}
    plan, risico = " ".join(plan.split()), "hoog" if risico == "hoog" else "laag"
    entry = {"id": secrets.token_hex(6), "gevraagd": datetime.now().isoformat(timespec="seconds"),
             "opdracht": opdracht, "plan": plan, "risico": risico,
             "hash": request_hash(opdracht, plan, risico)}
    with _requests_lock:
        REQUEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        with REQUEST_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    shown = False
    if APPROVAL_HOOK:
        try:
            shown = bool(APPROVAL_HOOK(entry))
        except Exception:
            shown = False
    return {"ok": True, "wacht_op": "Wouters goedkeuring in de app",
            "opmerking": ("Zeg dat hij het in de app moet goedkeuren; pas daarna begint Claude."
                          if shown else "De thuisserver is nu niet bereikbaar: de vraag verschijnt in "
                          "de app zodra hij terug is. Zeg dat erbij.")}


def _read_requests() -> list[dict]:
    if not REQUEST_FILE.exists():
        return []
    out = []
    for line in REQUEST_FILE.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _write_requests(entries: list[dict]) -> None:
    tmp = REQUEST_FILE.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries), encoding="utf-8")
    tmp.replace(REQUEST_FILE)


def pending_requests() -> list[dict]:
    """Requests still waiting for the app; older than a day = dropped."""
    from datetime import datetime

    with _requests_lock:
        entries = _read_requests()
        now = datetime.now()
        fresh = []
        for e in entries:
            try:
                age = (now - datetime.fromisoformat(e.get("gevraagd", ""))).total_seconds()
            except ValueError:
                continue
            if age < REQUEST_TTL_S:
                fresh.append(e)
        if len(fresh) != len(entries):
            _write_requests(fresh)
        return fresh


def decide(request_id: str, approved: bool, digest: str) -> str:
    """The app's answer (from the hub). Approved and unchanged -> the worker's queue."""
    from datetime import datetime

    with _requests_lock:
        entries = _read_requests()
        entry = next((e for e in entries if e.get("id") == request_id), None)
        if entry is None:
            return "onbekend"
        _write_requests([e for e in entries if e is not entry])
        ok = approved and digest == entry.get("hash") == request_hash(entry["opdracht"], entry["plan"], entry["risico"])
        if ok:
            QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with QUEUE_FILE.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return "goedgekeurd"
        line = {"tijd": datetime.now().isoformat(timespec="seconds"), "status": "afgewezen",
                "tekst": f"niet goedgekeurd in de app: {entry['opdracht'][:120]}"}
        with RESULT_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
        return "afgewezen"


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
    return {"wacht_op_goedkeuring_in_app": len(pending_requests()), "in_wachtrij": wachtrij,
            "laatste": klaar or "nog niets afgerond"}


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
    if not target.is_relative_to(REPO.resolve()):
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
             "--exclude=.env*", "-i", "-e", term, "--", "."],
            cwd=REPO, capture_output=True, text=True, timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return {"fout": str(exc)}
    # The term goes in after -e and before --, so it is always a pattern: a term like
    # "--include=*" used to become a grep option and search .env too (security audit, 24 Sep).
    # Filter on the file the hit is in, not on the text: a plain ".env" substring
    # test also throws away every line that mentions os.environ.
    hits = []
    for line in found.splitlines():
        path = line.split(":", 1)[0].removeprefix("./")
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


# --- The master plan (WILL-E.md) --------------------------------------------
# rsync puts it inside the repo on the Pi; on the Mac it sits one level up.
PLAN_CANDIDATES = (REPO / "WILL-E.md", REPO.parent / "WILL-E.md")
STEP = re.compile(r"^[A-KS]\d{1,2}$", re.I)
OPEN_STEP = re.compile(r"^- \[ \] \*\*([A-KS]\d{1,2})\b")


def _plan_lines() -> list[str] | None:
    for path in PLAN_CANDIDATES:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace").splitlines()
    return None


def _clip(text: str, first_line: int) -> dict:
    out = {"regel": first_line, "tekst": text[:MAX_CHARS]}
    if len(text) > MAX_CHARS:
        out["afgekapt"] = f"lees verder met lees_code pad WILL-E.md vanaf_regel {first_line + text[:MAX_CHARS].count(chr(10))}"
    return out


def _section(lines: list[str], start: int) -> str:
    level = len(lines[start]) - len(lines[start].lstrip("#"))
    end = start + 1
    while end < len(lines):
        head = lines[end]
        if head.startswith("#") and len(head) - len(head.lstrip("#")) <= level:
            break
        end += 1
    return "\n".join(lines[start:end]).strip()


def lees_plan(onderdeel: str = "") -> dict:
    lines = _plan_lines()
    if lines is None:
        return {"fout": "WILL-E.md staat niet op de Pi - make sync zet hem erop"}
    want = onderdeel.strip().lstrip("§").strip().rstrip(".")

    if not want or want.lower() in ("huidig", "huidige stap", "nu", "volgende", "current"):
        first, per_phase = None, {}
        for n, line in enumerate(lines, 1):
            found = OPEN_STEP.match(line)
            if not found or line.startswith("- [ ] ~~"):
                continue
            first = first or (n, line)
            per_phase.setdefault(found.group(1)[0].upper(), line[6:200])
        if not first:
            return {"huidige_stap": "alle stappen zijn afgevinkt"}
        return {"huidige_stap": _clip(first[1], first[0]), "eerstvolgende_per_fase": per_phase}

    if STEP.match(want):
        key = want.upper()
        hits = [(n, l) for n, l in enumerate(lines, 1)
                if re.search(rf"\*\*{key}(\*\*|\s)", l) or l.startswith(f"| {key} |")]
        found = [_clip(l, n) for n, l in hits[:3]]
        # CAD steps have their own section too ("### 9.4 C3 — REF models").
        found += [_clip(_section(lines, n), n + 1) for n, l in enumerate(lines)
                  if l.startswith("#") and re.search(rf"\b{key}\b", l)][:1]
        if found:
            return {"gevonden": found}

    if re.match(r"^\d{1,2}(\.\d{1,2})?$", want):
        for n, line in enumerate(lines):
            if re.match(rf"^#+ {re.escape(want)}[ .]", line):
                return _clip(_section(lines, n), n + 1)

    for n, line in enumerate(lines):
        if line.startswith("#") and want.lower() in line.lower():
            return _clip(_section(lines, n), n + 1)
    hits = [f"{n}: {l[:200]}" for n, l in enumerate(lines, 1) if want.lower() in l.lower()]
    return {"treffers": hits[:12] or "niets gevonden", "aantal": len(hits)}


# --- Logs --------------------------------------------------------------------
SECRET = re.compile(r"(key|token|password|secret)=[^&\s\"']+", re.I)


def lees_logs(dienst: str = "willie-voice", zoek: str = "") -> dict:
    unit = dienst if dienst in ("willie", "willie-voice") else "willie-voice"
    try:
        out = subprocess.run(["journalctl", "-u", unit, "-n", "300", "--no-pager", "-o", "short"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return {"fout": str(exc)}
    lines = [SECRET.sub(r"\1=***", l) for l in out.splitlines()]
    if zoek.strip():
        lines = [l for l in lines if zoek.strip().lower() in l.lower()]
    tail = "\n".join(lines[-30:])
    return {"dienst": unit, "regels": tail[-MAX_CHARS:] or "leeg"}


# --- Ask Claude Code on the Mac ------------------------------------------------
# Same pattern as verbeter_jezelf: the Pi only writes a line of text; the worker on
# the Mac polls, runs a read-only Claude Code and writes the answer back.
ASK_FILE = Path(os.environ.get("WILLIE_ASK_QUEUE", REPO / ".local" / "claude_questions.jsonl"))
ANSWER_FILE = Path(os.environ.get("WILLIE_ASK_ANSWERS", REPO / ".local" / "claude_answers.jsonl"))


def vraag_claude(vraag: str) -> dict:
    vraag = " ".join(vraag.split())
    if len(vraag) < 10:
        return {"fout": "te vaag, stel een volledige vraag"}
    from datetime import datetime

    ASK_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = {"gevraagd": datetime.now().isoformat(timespec="seconds"), "vraag": vraag}
    with ASK_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"ok": True, "opmerking": "Claude is ermee bezig, 1 tot 5 minuten. De laptop moet aan staan."}


def antwoord_claude() -> dict:
    lopend = 0
    if ASK_FILE.exists():
        lopend = sum(1 for line in ASK_FILE.read_text(encoding="utf-8").splitlines() if line.strip())
    antwoorden = []
    if ANSWER_FILE.exists():
        for line in ANSWER_FILE.read_text(encoding="utf-8").splitlines()[-2:]:
            try:
                antwoorden.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return {"nog_niet_opgehaald_door_laptop": lopend, "laatste": antwoorden or "nog geen antwoord"}


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


# Called right before a power-off/restart (set by willie/remote.py): tell the phone app it is
# planned, play the sleep chime, show the sleep face.
POWER_HOOK = None
POWER_DELAY_S = 6.0            # time to say goodbye and to send the tool result first


def zet_uit(actie: str = "uit", **_ignored) -> dict:
    """Voice: only after a finger on the screen (Face.confirm). The phone app has its own
    "are you sure?" and calls power() directly (willie/remote.py)."""
    if actie not in ("uit", "herstart"):
        return {"fout": "actie moet 'uit' of 'herstart' zijn"}
    if CONFIRM is None:
        return {"fout": "Ik kan dit nu niet laten bevestigen (geen aanraakscherm). Het kan wel vanuit de app."}
    vraag = "Helemaal uitzetten?" if actie == "uit" else "Herstarten?"
    answer = CONFIRM(vraag, 20.0)
    if answer is not True:
        return {"niet_gedaan": "Geannuleerd op het scherm." if answer is False else "Niemand hield het scherm vast; ik doe niets."}
    return power(actie)


def power(actie: str) -> dict:
    import time

    command = ["sudo", "-n", "systemctl", "poweroff" if actie == "uit" else "reboot"]

    def later():
        time.sleep(POWER_DELAY_S)
        if POWER_HOOK:
            try:
                POWER_HOOK(actie)
            except Exception:
                pass
        subprocess.run(command, check=False)

    threading.Thread(target=later, daemon=True).start()
    return {"ok": True, "over_seconden": POWER_DELAY_S,
            "let_op": "Na uitzetten kan hij alleen met de schakelaar weer aan." if actie == "uit" else "Terug over ongeveer een minuut."}


def garagemodus(aan: bool) -> dict:
    """K1. Switching on from a normal conversation ends it after his answer; the voice loop
    then opens the garage conversation. Switching off ends the garage conversation."""
    from willie.voice import garage

    garage.set_enabled(bool(aan))
    WRAP_UP.set()
    if aan:
        return {"ok": True, "zeg": "Kort: garagemodus aan, ik luister nu zonder 'Hey Willie', alleen naar jou."}
    return {"ok": True, "zeg": "Kort: garagemodus uit, zeg weer 'Hey Willie'."}


def zet_volume(niveau: float) -> dict:
    from willie.audio import speech

    from willie.config import Config

    niveau = max(0.02, min(0.8, float(niveau)))
    Config().update({"voice": {"volume": niveau}})     # saved: survives a restart
    speech._cached = (0.0, niveau)
    return {"ok": True, "volume": niveau}


from willie import skills  # noqa: E402  (H1: home systems, each one switchable)

HANDLERS = {
    "kijk": kijk,
    "lees_plan": lees_plan,
    "lees_logs": lees_logs,
    "vraag_claude": vraag_claude,
    "antwoord_claude": antwoord_claude,
    "lees_code": lees_code,
    "zoek_in_code": zoek_in_code,
    "lijst_code": lijst_code,
    "onthoud": onthoud,
    "herinner": herinner,
    "status": status,
    "zet_volume": zet_volume,
    "garagemodus": garagemodus,
    "zet_uit": zet_uit,
    "verbeter_jezelf": verbeter_jezelf,
    "verbeteringen_status": verbeteringen_status,
}


def declarations() -> list[dict]:
    """The robot's own tools + those of every skill that is switched on (H1)."""
    return DECLARATIONS + skills.declarations()


def call(name: str, arguments: dict) -> dict:
    handler = HANDLERS.get(name)
    if not handler:
        handler, refused = skills.handler(name)
        if refused:
            return {"fout": refused}
    if not handler:
        return {"fout": f"onbekende tool {name}"}
    try:
        return handler(**(arguments or {}))
    except TypeError as exc:
        return {"fout": f"verkeerde argumenten: {exc}"}
    except Exception as exc:  # a tool must never take the session down
        return {"fout": f"{type(exc).__name__}: {exc}"}


def remembered() -> str:
    """Long-term memory, to paste into the system prompt at session start (D10)."""
    return memory.context()
