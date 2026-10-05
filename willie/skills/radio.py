"""Live internet radio on WILL-E's own speaker (U5b, added 5 Oct, Wouter).

A second music source next to Spotify (skills/spotify.py). One setting, `music.source`
(`spotify` | `radio`), decides which one answers play / pause / skip / volume. Saying "zet de
radio aan" or "terug naar Spotify", or changing the setting in the dashboard or app, switches;
starting one source stops the other, so the two never play together.

The player is one `mpv --no-video` process into the ALSA device `willie_music` (config/asound.conf,
the same path librespot uses). There is no pause on a live stream, so "pause for talk" kills mpv
(the card is free at once) and the end of the conversation starts the station again. The speech
handover is the same as Spotify's: TALKING / pause_for_talk / after_talk / hold / stop.

Stations: setting `music.stations` = "Name=stream URL; Name=stream URL" (MP3/AAC).
Install: `make radio-setup` (apt install mpv).
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import shutil
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("willie.radio")

LABEL = "Radio - live internet radio on his own speaker (mpv)"

REPO = Path(__file__).resolve().parents[2]
STATE_DIR = REPO / ".local"
DEVICE = "alsa/willie_music"
START_CHECK_S = 2.0         # a stream that is dead exits within this time; say so then
IPC_TIMEOUT_S = 0.4
TITLE_EVERY_S = 5.0         # how often the current song (ICY title) is asked from mpv
ALTERNATE_S = 5.0           # the face line has room for ~32 characters: station and song take turns

PRESETS = {"normaal": (0, 0), "warm": (4, -2), "stem": (-3, 3), "bas": (6, 1)}   # (bass, treble) dB

DECLARATIONS = [
    {
        "name": "speel_radio",
        "description": (
            "Zet live radio aan op je eigen speaker, of wissel naar een andere zender. Gebruik dit "
            "bij 'zet de radio aan', 'Radio 10', 'Qmusic' enzovoort. Zonder 'zender' komt de zender "
            "die het laatst speelde. Dit zet Spotify uit: er speelt steeds maar een bron. Wil Wouter "
            "terug naar Spotify, gebruik dan speel_muziek (leeg = verder waar het was). In een "
            "gesprek start de radio zodra je uitgepraat bent en stopt het gesprek: zeg dus alleen "
            "kort wat je opzet (een halve zin), stel geen vraag terug."
        ),
        "parameters": {
            "type": "object",
            "properties": {"zender": {"type": "string", "description": "Naam van de zender. Leeg = de laatste."}},
        },
    },
    {
        "name": "radio_klank",
        "description": (
            "Verander de klank van de radio: 'bas' en 'hoog' in dB (-10 tot +8, 0 = neutraal), of een "
            "'preset': normaal, warm, stem (voor praatzenders), bas. Gebruik dit bij 'meer bas', "
            "'minder hoge tonen', 'maak het warmer'. Vraag zonder argumenten naar de huidige stand. "
            "Per stap: ongeveer 3 dB; boven +6 gaat zijn kleine speaker vervormen. Werkt alleen voor de radio."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "bas": {"type": "integer", "description": "Bas in dB, -10..8"},
                "hoog": {"type": "integer", "description": "Hoge tonen in dB, -10..8"},
                "preset": {"type": "string", "enum": list(PRESETS), "description": "Vaste instelling."},
            },
        },
    },
    {
        "name": "radio_zenders",
        "description": "Welke radiozenders je kent, en welke er nu speelt.",
        "parameters": {"type": "object", "properties": {}},
    },
]
NAMES = {d["name"] for d in DECLARATIONS}


class RadioError(RuntimeError):
    """A failure with a short Dutch sentence he can say."""


# ------------------------------------------------------------------ settings

_config = None


def _settings():
    global _config
    from willie.config import Config
    if _config is None:
        _config = Config()
    else:
        _config.reload()
    return _config


def source() -> str:
    try:
        return str(_settings().get("music.source"))
    except Exception:
        return "spotify"


def active() -> bool:
    """The radio is the chosen music source."""
    return source() == "radio"


def set_source(name: str) -> None:
    try:
        cfg = _settings()
        if cfg.get("music.source") != name:
            cfg.update({"music": {"source": name}})
            log.info("music source -> %s", name)
    except Exception as exc:
        log.warning("music source not saved: %s", exc)


def stations() -> dict[str, str]:
    """{name: stream URL} from the setting; a malformed entry is skipped."""
    try:
        raw = str(_settings().get("music.stations") or "")
    except Exception:
        return {}
    found = {}
    for entry in raw.split(";"):
        name, sep, url = entry.partition("=")
        name, url = name.strip(), url.strip()
        if sep and name and url.startswith(("http://", "https://")):
            found[name] = url
    return found


def configured() -> bool:
    return bool(stations()) and shutil.which("mpv") is not None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def find(zender: str) -> tuple[str, str]:
    """(name, url): exact name ignoring case/spaces/dashes ("Q music", "non stop"), else a name
    that contains the words, else the closest spelling. Empty = the station that played last."""
    known = stations()
    if not known:
        raise RadioError("Er staan geen radiozenders in de instellingen (music.stations).")
    wanted = _norm(zender)
    if not wanted:
        try:
            last = str(_settings().get("music.station"))
        except Exception:
            last = ""
        name = last if last in known else next(iter(known))
        return name, known[name]
    by_norm = {_norm(n): n for n in known}
    if wanted in by_norm:
        name = by_norm[wanted]
        return name, known[name]
    contains = [n for k, n in by_norm.items() if wanted in k or k in wanted]
    if contains:
        name = min(contains, key=len)           # "sterren" -> the shortest station that has it
        return name, known[name]
    close = difflib.get_close_matches(wanted, list(by_norm), n=1, cutoff=0.6)
    if close:
        name = by_norm[close[0]]
        return name, known[name]
    raise RadioError(f"Ik ken de zender '{zender}' niet. Ik ken: {', '.join(known)}.")


# ------------------------------------------------------------------ the mpv process

SWITCH = threading.Lock()       # one source switch at a time (tools, the setting watcher)
_lock = threading.RLock()
_proc: subprocess.Popen | None = None
_current = ""                   # station name of the running (or paused-for-talk) stream
_title = {"text": "", "at": 0.0}


def _sock() -> Path:
    return STATE_DIR / "radio.sock"


def playing() -> bool:
    return _proc is not None and _proc.poll() is None


def card_busy() -> bool:
    return playing()


def _volume() -> int:
    try:
        return int(_settings().get("music.radio_volume"))
    except Exception:
        return 50


_applied_sound = ""


def _filter() -> str:
    """The mpv audio filter for the bass/treble settings; "" when both are 0. The limiter keeps the
    boost from clipping on a loud station."""
    global _applied_sound
    try:
        bass, treble = int(_settings().get("music.bass")), int(_settings().get("music.treble"))
    except Exception:
        bass = treble = 0
    _applied_sound = f"{bass},{treble}"
    if not bass and not treble:
        return ""
    return f"lavfi=[bass=g={bass},treble=g={treble},alimiter=limit=0.9]"


def refresh_sound() -> None:
    """The bass/treble setting changed (dashboard, app, voice) while the radio plays: apply it live."""
    if not playing():
        return
    before = _applied_sound
    flt = _filter()
    if _applied_sound != before:
        _ipc("set_property", "af", flt)


def _spawn(name: str, url: str) -> None:
    global _proc, _current
    with _lock:
        _kill()
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _sock().unlink(missing_ok=True)
        # no config, no scripts (no ytdl/python startup), a small cache: keeps RSS low on the Pi
        command = ["mpv", "--no-config", "--no-video", "--no-terminal", "--really-quiet",
                   "--load-scripts=no", "--ytdl=no", "--idle=no",
                   f"--audio-device={DEVICE}", f"--volume={_volume()}", f"--af={_filter()}",
                   "--cache=yes", "--cache-secs=10", "--demuxer-max-bytes=2MiB", "--demuxer-max-back-bytes=0",
                   "--network-timeout=10",
                   "--stream-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_delay_max=5",
                   f"--input-ipc-server={_sock()}", "--", url]
        try:
            _proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        except OSError as exc:
            _proc = None
            raise RadioError("De radiospeler (mpv) staat er niet op; draai 'make radio-setup'.") from exc
        _current = name
        _title.update(text="", at=0.0)
        try:
            _settings().update({"music": {"station": name}})
        except Exception as exc:
            log.warning("station not saved: %s", exc)


def _kill() -> None:
    global _proc
    proc, _proc = _proc, None
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _ipc(*command) -> object:
    """One mpv JSON IPC command; the answer's data, or None. Never raises."""
    if not playing():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(IPC_TIMEOUT_S)
            s.connect(str(_sock()))
            s.sendall(json.dumps({"command": list(command), "request_id": 1}).encode() + b"\n")
            for line in s.makefile("r"):
                reply = json.loads(line)
                if reply.get("request_id") == 1:
                    return reply.get("data") if reply.get("error") == "success" else None
    except (OSError, ValueError):
        pass
    return None


def _start(name: str, url: str) -> None:
    """Start a stream and make sure it did not die at once (bad URL, no internet)."""
    _spawn(name, url)
    end = time.monotonic() + START_CHECK_S
    while time.monotonic() < end:
        if not playing():
            break
        time.sleep(0.1)
    if not playing():
        raise RadioError(f"De zender {name} geeft geen geluid (stream onbereikbaar?).")


def _song() -> str:
    """The current song as the stream announces it (ICY title), else mpv's media title when it
    looks like "ARTIST - TITLE"; "" when the stream says nothing."""
    text = _ipc("get_property", "metadata/by-key/icy-title")
    if not (isinstance(text, str) and text.strip()):
        text = _ipc("get_property", "media-title")
        if not (isinstance(text, str) and " - " in text and not text.startswith("http")):
            return ""
    song = text.strip()
    return "" if _norm(song) == _norm(_current) else song


def song() -> str:
    """The cached song, refreshed every TITLE_EVERY_S seconds (cheap enough for the face loop)."""
    if not playing():
        return ""
    now = time.monotonic()
    if now - _title["at"] >= TITLE_EVERY_S:
        _title["at"] = now
        _title["text"] = _song()
    return _title["text"]


def now_playing() -> str:
    """For the face's bottom line while the radio plays and no conversation holds it, else "":
    the station, and when the stream announces a song, the song in turns with the station."""
    if TALKING or not playing():
        return ""
    text = song()
    if text and int(time.monotonic() / ALTERNATE_S) % 2:
        return text
    return _current


# ------------------------------------------------------------------ talking and music take turns

TALKING = False            # a conversation holds the speaker
_resume = False            # the radio was playing when the conversation started
_pending = None            # (description, callable): what to start when the conversation ends


def _wrap_up() -> None:
    from willie.voice import tools as voice_tools
    voice_tools.WRAP_UP.set()


def pause_for_talk() -> None:
    """Wake word heard: stop the stream (the card is free when this returns). Never raises."""
    global TALKING, _resume, _pending
    TALKING, _resume, _pending = True, False, None
    if playing():
        _resume = True
        _kill()


def after_talk(resume: bool = True) -> None:
    """Conversation over: start what was asked, else the station that was playing. Never raises."""
    global TALKING, _resume, _pending
    action, was_playing, name = _pending, _resume, _current
    TALKING, _resume, _pending = False, False, None
    try:
        if action:
            log.info("radio after talk: %s", action[0])
            action[1]()
        elif was_playing and resume and name:
            _start(*find(name))
    except RadioError as exc:
        log.warning("radio after talk: %s", exc)


@contextmanager
def hold():
    """Around speech outside a conversation (reminders, "say" from the phone, chimes)."""
    if TALKING or not playing():
        yield
        return
    name = _current
    _kill()
    try:
        yield
    finally:
        try:
            _spawn(*find(name))
        except RadioError as exc:
            log.warning("hold resume: %s", exc)


def stop() -> None:
    """Sleep mode, or the other source takes over: radio off, and it does not come back."""
    global _resume, _pending
    _resume, _pending = False, None
    _kill()


def _do(description: str, action) -> dict:
    """Run a player action now, or - in a conversation - once it has ended."""
    global _pending, _resume
    if TALKING:
        _pending, _resume = (description, action), False
        _wrap_up()
        return {"ok": True, "start": "zodra je uitgepraat bent (het gesprek stopt dan)"}
    action()
    return {"ok": True, "start": "nu"}


# ------------------------------------------------------------------ tools

def speel_radio(zender: str = "") -> dict:
    from willie.skills import spotify
    try:
        name, url = find(zender)
        if shutil.which("mpv") is None:
            raise RadioError("De radiospeler (mpv) staat er niet op; draai 'make radio-setup'.")
        with SWITCH:
            set_source("radio")
            spotify.stop()              # one source at a time (in a conversation it only drops its resume)
            result = _do(name, lambda: _start(name, url))
        return {**result, "speelt": name}
    except RadioError as exc:
        return {"fout": str(exc)}


def radio_klank(bas: int | None = None, hoog: int | None = None, preset: str = "") -> dict:
    try:
        if preset:
            if preset not in PRESETS:
                return {"fout": f"Onbekende preset. Kies uit: {', '.join(PRESETS)}."}
            bas, hoog = PRESETS[preset]
        changes = {}
        for key, value in (("bass", bas), ("treble", hoog)):
            if value is not None:
                changes[key] = max(-10, min(8, int(value)))
        if changes:
            _settings().update({"music": changes})
            refresh_sound()
        cfg = _settings()
        stand = {"bas": cfg.get("music.bass"), "hoog": cfg.get("music.treble")}
        if max(stand.values()) > 6:
            stand["let_op"] = "boven +6 dB kan zijn speaker vervormen"
        if not playing():
            stand["opmerking"] = "geldt zodra de radio aan staat"
        return {"ok": True, **stand}
    except (ValueError, TypeError) as exc:
        return {"fout": str(exc)}


def radio_zenders() -> dict:
    return {"zenders": list(stations()), "speelt": _current if playing() else "niets",
            "bron": source()}


# The next three are what the generic music tools do while the radio is the source
# (willie/skills/spotify.py hands over to them).

def bediening(actie: str) -> dict:
    global _resume, _pending
    if actie == "pauze":
        if TALKING:
            _resume, _pending = False, None
            return {"ok": True, "muziek": "blijft uit na het gesprek"}
        _kill()
        return {"ok": True, "muziek": "radio uit"}
    if actie == "hervat":
        return speel_radio("")
    if actie in ("volgende", "vorige"):
        known = list(stations())
        if not known:
            return {"fout": "Er staan geen radiozenders in de instellingen."}
        i = known.index(_current) if _current in known else (known.index(_last()) if _last() in known else 0)
        return speel_radio(known[(i + (1 if actie == "volgende" else -1)) % len(known)])
    return {"fout": f"onbekende actie {actie}"}


def _last() -> str:
    try:
        return str(_settings().get("music.station"))
    except Exception:
        return ""


def volume(procent: int) -> dict:
    try:
        procent = max(0, min(100, int(procent)))
    except (TypeError, ValueError) as exc:
        return {"fout": str(exc)}
    try:
        _settings().update({"music": {"radio_volume": procent}})
    except Exception as exc:
        return {"fout": f"Volume niet opgeslagen: {exc}"}
    _ipc("set_property", "volume", procent)
    return {"ok": True, "muziekvolume": procent}


def wat_speelt_er() -> dict:
    return {"bron": "radio", "nummer": _current or _last() or None,
            "artiest": song() or None, "speelt_nu": playing(),
            "gepauzeerd_voor_gesprek": TALKING and _resume,
            "muziekvolume": _volume()}


HANDLERS = {"speel_radio": speel_radio, "radio_zenders": radio_zenders, "radio_klank": radio_klank}


def card() -> dict:
    if shutil.which("mpv") is None:
        return {"radio": "mpv niet geinstalleerd (make radio-setup)"}
    return {"bron": source(), "speelt": _current if playing() else "uit", "zenders": ", ".join(stations())}
