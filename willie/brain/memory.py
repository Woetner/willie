"""WILL-E's long-term memory (23 Sep, Wouter: "store as much as possible, so he knows
everything from past sessions").

Where it lives (24 Sep, Wouter: "don't save it on the card, save it on the server"): on
the home server, in ~/homeserver-data/memory/ (WILLIE_MEMORY_DIR in hub.service). The hub
runs this same module there. On the robot, tools/willie_voice.py sets ON_SERVER and
HUB_CALL, and every read and write below goes to the hub over MQTT (willie/hub/call).
With the server down he talks without his memory, and a finished conversation waits in
RAM until the next one - nothing is written to the SD card. A reboot in between loses it.

Three layers, all plain text files (on the Mac, for tests and bench scripts, under .local/memory/):

- sessions/YYYY-MM-DD.md  every conversation, word for word (both sides, from the Live
                          API's transcriptions). Never rewritten, never trimmed.
- facts.md                what is still true tomorrow: people, projects, preferences,
                          measurements, plans. `onthoud` appends; after each session a
                          text model merges the new conversation into it.
- summaries.md            one short line per conversation, newest last.

At session start `context()` pastes facts + as many recent summaries as fit in
memory.context_chars into the prompt. Everything older is still one `herinner` (search)
away, so nothing he has heard is lost - it just is not all in the prompt at once.

RAM: nothing resident. The merge is one HTTPS call in a thread after the session closes.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger("willie.memory")

REPO = Path(__file__).resolve().parents[2]
DIR = Path(os.environ.get("WILLIE_MEMORY_DIR", REPO / ".local" / "memory"))
FACTS = DIR / "facts.md"
SUMMARIES = DIR / "summaries.md"
SESSIONS = DIR / "sessions"
PENDING = DIR / "pending.md"                    # conversations whose merge failed, retried next time
LEGACY = REPO / "config" / "memory.md"          # before 23 Sep

# Tried in order: a busy model (HTTP 503, seen on the Pi 23 Sep) falls through to the next.
MODELS = tuple(filter(None, (os.environ.get("WILLIE_MEMORY_MODEL"), "gemini-flash-latest",
                             "gemini-flash-lite-latest", "gemini-3.5-flash-lite")))
DEFAULT_CONTEXT_CHARS = 24_000
SEARCH_CHARS = 3_000
FACTS_HEADER = "# Wat WILL-E weet\n\n"

ON_SERVER = False               # the robot: memory only on the home server
HUB_CALL = None                 # fn(name, args, timeout) -> dict, set with ON_SERVER
UNSENT_MAX = 20                 # conversations kept in RAM while the server is away
_unsent: list[dict] = []


def _server(name: str, args: dict, timeout: float) -> dict:
    if HUB_CALL is None:
        return {"fout": "Mijn geheugen staat op de thuisserver, en die is nu niet bereikbaar."}
    result = HUB_CALL(name, args, timeout)
    return result if isinstance(result, dict) else {"fout": "onverwacht antwoord van de server"}


def _ensure() -> None:
    SESSIONS.mkdir(parents=True, exist_ok=True)
    if not FACTS.exists():
        legacy = LEGACY.read_text(encoding="utf-8") if LEGACY.exists() else ""
        body = "\n".join(line for line in legacy.splitlines() if line.startswith("- "))
        FACTS.write_text(FACTS_HEADER + (body + "\n" if body else ""), encoding="utf-8")


def context_chars() -> int:
    try:
        from willie.config import Config
        return int(Config().get("memory.context_chars"))
    except Exception:
        return DEFAULT_CONTEXT_CHARS


# ---------------------------------------------------------------- writing
def note(text: str) -> dict:
    """The `onthoud` tool: one fact, appended straight away."""
    text = " ".join(text.split())
    if not text:
        return {"fout": "lege notitie"}
    if ON_SERVER:
        return _server("geheugen_onthoud", {"notitie": text}, 10)
    _ensure()
    existing = FACTS.read_text(encoding="utf-8")
    if text.lower() in existing.lower():
        return {"ok": True, "opmerking": "wist ik al"}
    FACTS.write_text(f"{existing.rstrip()}\n- {date.today().isoformat()}: {text}\n", encoding="utf-8")
    return {"ok": True}


def format_transcript(turns: list[tuple[str, str]]) -> str:
    return "\n".join(f"{'Wouter' if who == 'user' else 'WILL-E'}: {text}" for who, text in turns)


def save_session(turns: list[tuple[str, str]], started: datetime | None = None) -> Path | None:
    """Append one conversation to today's session file. Returns the file, or None if empty."""
    text = format_transcript(turns).strip()
    if not text:
        return None
    _ensure()
    started = started or datetime.now()
    path = SESSIONS / f"{started:%Y-%m-%d}.md"
    with path.open("a", encoding="utf-8") as out:
        out.write(f"\n## {started:%H:%M}\n\n{text}\n")
    return path


def digest(turns: list[tuple[str, str]], started: datetime | None = None, api_key: str | None = None) -> dict:
    """Save the conversation, then let a text model summarise it and merge new facts into
    facts.md. Runs after the session, in a thread; never raises."""
    started = started or datetime.now()
    if ON_SERVER:
        return _send(turns, started)
    try:
        if save_session(turns, started) is None:
            return {"ok": False, "reden": "leeg gesprek"}
    except OSError as exc:
        log.warning("memory: could not save the session: %s", exc)
        return {"fout": str(exc)}
    conversation = f"({started:%Y-%m-%d %H:%M})\n{format_transcript(turns)}"
    key = api_key or os.environ.get("GEMINI_API_KEY", "")
    backlog = PENDING.read_text(encoding="utf-8") if PENDING.exists() else ""
    if not key:
        _pend(conversation)
        return {"ok": True, "reden": "geen sleutel: alleen letterlijk opgeslagen"}
    try:
        result = _merge(f"{backlog}\n\n{conversation}".strip(), key)
    except (RuntimeError, ValueError, KeyError, IndexError) as exc:
        log.warning("memory: merge failed, the transcript is saved and retried next time: %s", exc)
        _pend(conversation)
        return {"ok": True, "fout": str(exc)}
    PENDING.unlink(missing_ok=True)
    summary = " ".join(str(result.get("samenvatting", "")).split())
    if summary:
        with SUMMARIES.open("a", encoding="utf-8") as out:
            out.write(f"- {started:%Y-%m-%d %H:%M}: {summary}\n")
    facts = str(result.get("feiten", "")).strip()
    old = FACTS.read_text(encoding="utf-8")
    # A merge that throws away a lot is more likely a model slip than a clean-up: keep the old
    # file then. The transcripts hold everything anyway, so this errs on keeping.
    if facts and len(facts) >= 0.8 * len(old.strip()):
        shutil.copyfile(FACTS, FACTS.with_suffix(".md.bak"))
        body = facts if facts.startswith("#") else FACTS_HEADER + facts
        FACTS.write_text(body.rstrip() + "\n", encoding="utf-8")
    elif facts:
        log.warning("memory: merge shrank facts.md from %d to %d chars - kept the old one", len(old), len(facts))
    return {"ok": True, "samenvatting": summary}


def _send(turns, started: datetime) -> dict:
    """Robot: the conversation goes to the server, which saves and merges it. Server away:
    it waits in RAM (never on the card) and goes along with the next conversation."""
    if turns:
        _unsent.append({"gesprek": [list(t) for t in turns], "begonnen": started.isoformat(timespec="seconds")})
        del _unsent[:-UNSENT_MAX]
    result: dict = {"ok": False, "reden": "leeg gesprek"}
    while _unsent:
        result = _server("geheugen_gesprek", _unsent[0], 120)
        if "fout" in result and not result.get("ok"):
            log.warning("memory: server did not take the conversation (%d waiting in RAM): %s",
                        len(_unsent), result["fout"])
            return result
        _unsent.pop(0)
    return result


def remote_digest(gesprek: list, begonnen: str = "") -> dict:
    """Server side of _send (hub/app.py): one conversation from the robot."""
    try:
        started = datetime.fromisoformat(begonnen) if begonnen else datetime.now()
    except ValueError:
        started = datetime.now()
    turns = [(str(who), str(text)) for who, text in gesprek if who in ("user", "model", "willie")]
    return digest(turns, started)


def _pend(conversation: str) -> None:
    with PENDING.open("a", encoding="utf-8") as out:
        out.write(f"\n\n{conversation}")


MERGE_PROMPT = """Je beheert het geheugen van WILL-E, de huisrobot van Wouter.

Hieronder staat (1) wat WILL-E al weet en (2) een nieuw gesprek.

Geef JSON terug met twee velden:
- "samenvatting": één zin (max 30 woorden) over waar het gesprek (of de gesprekken) over ging en wat eruit kwam.
- "feiten": het VOLLEDIGE bijgewerkte geheugen als Markdown. Neem alles over wat er al stond,
  en voeg toe wat nieuw is en later nog nuttig kan zijn: over Wouter (voorkeuren, gewoontes,
  mensen, afspraken, plannen), zijn projecten (WILL-E, Kever, Tomos, printer, ESP32, ...),
  maten, instellingen, beslissingen, open vragen, en wat WILL-E beloofde te doen.
  Werk verouderde feiten bij in plaats van ze dubbel te laten staan. Groepeer onder kopjes
  (## Wouter, ## Projecten, ## Huis, ## Afspraken, ...). Elke regel begint met "- " en
  eindigt met de datum waarop het voor het laatst klopte (JJJJ-MM-DD). Gooi niets weg dat nog
  waar kan zijn. Geen smalltalk, geen dingen die alleen voor dat moment golden.
  Zet relatieve tijden ("zaterdag", "morgen", "volgende week") om naar een echte datum.

Vandaag is {today}.

=== WAT WILL-E AL WEET ===
{facts}

=== NIEUW GESPREK ===
{conversation}
"""


def _merge(conversation: str, key: str, timeout: float = 30.0) -> dict:
    _ensure()
    prompt = MERGE_PROMPT.format(today=date.today().isoformat(),
                                 facts=FACTS.read_text(encoding="utf-8"), conversation=conversation)
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
    }).encode()
    errors = []
    for model in MODELS:
        request = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            data=body, headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                answer = json.loads(response.read())
            result = json.loads(answer["candidates"][0]["content"]["parts"][0]["text"])
        except urllib.error.HTTPError as exc:
            errors.append(f"{model}: HTTP {exc.code}")
            continue
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError) as exc:
            errors.append(f"{model}: {exc}")
            continue
        if isinstance(result, dict):
            return result
        errors.append(f"{model}: answer is not an object")
    raise RuntimeError("; ".join(errors))


# ---------------------------------------------------------------- reading
def context(budget: int | None = None) -> str:
    """For the system prompt: all facts, then the newest summaries that still fit."""
    budget = context_chars() if budget is None else budget
    if ON_SERVER:
        # Asked at the start of every conversation: a slow server must not hold him up long.
        return str(_server("geheugen_context", {"budget": budget}, 3).get("context", ""))
    if not DIR.exists() and not LEGACY.exists():
        return ""
    _ensure()
    facts = FACTS.read_text(encoding="utf-8").strip()
    if len(facts) <= len(FACTS_HEADER.strip()):
        facts = ""
    if len(facts) > budget:
        facts = facts[-budget:]                       # newest lines are at the end
    parts = [f"Wat je weet (je eigen geheugen, uit eerdere gesprekken):\n{facts}"] if facts else []
    room = budget - len(facts)
    if SUMMARIES.exists() and room > 200:
        picked = []
        for line in reversed(SUMMARIES.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            if len(line) + 1 > room:
                break
            picked.append(line)
            room -= len(line) + 1
        if picked:
            parts.append("Eerdere gesprekken (oudste eerst):\n" + "\n".join(reversed(picked)))
    if not parts:
        return ""
    return ("\n\n" + "\n\n".join(parts) +
            "\n\nWeet je iets niet meer precies, of vraagt hij naar een eerder gesprek: gebruik herinner.")


def search(term: str) -> dict:
    """The `herinner` tool: search every word ever said, plus the facts file."""
    words = [w for w in re.findall(r"\w+", term.lower()) if len(w) > 2]
    if not words:
        return {"fout": "geen zoekterm"}
    if ON_SERVER:
        return _server("geheugen_zoek", {"term": term}, 10)
    if not DIR.exists():
        return {"gevonden": [], "opmerking": "nog geen geheugen"}
    hits: list[str] = []
    files = [FACTS, SUMMARIES] + sorted(SESSIONS.glob("*.md"), reverse=True)
    for path in files:
        if not path.exists():
            continue
        day = path.stem if path.parent == SESSIONS else path.name
        heading = ""
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                heading = line[3:].strip()
                continue
            low = line.lower()
            if sum(w in low for w in words) >= max(1, (len(words) + 1) // 2):
                where = f"{day} {heading}".strip() if path.parent == SESSIONS else day
                hits.append(f"[{where}] {line.strip()}")
    if not hits:
        return {"gevonden": [], "opmerking": "niets gevonden"}
    out, size = [], 0
    for hit in hits:
        if size + len(hit) > SEARCH_CHARS:
            break
        out.append(hit)
        size += len(hit)
    return {"gevonden": out, "totaal": len(hits)}
