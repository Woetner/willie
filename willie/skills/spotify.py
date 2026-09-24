"""Spotify on WILL-E's own speaker (added 24 Sep, Wouter).

Two halves:
  - willie-spotify.service runs librespot: WILL-E is a Spotify Connect speaker called
    "WILL-E" (install: `make spotify-setup`, then pick WILL-E once in the Spotify app).
  - this skill steers it through the Spotify Web API: search, play, pause, skip, volume.
    Secrets in .env (`make spotify-login` asks for them on the Pi, never in chat):
    SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REFRESH_TOKEN.

The sound card has one player at a time (aplay opens the hardware directly), and the
mics would hear the music anyway, so music and talking take turns:
  - "Hey Willie" -> pause_for_talk(): the music pauses, librespot closes the card.
  - the conversation ends -> after_talk(): the music resumes, or starts what was asked.
  - "speel X" in a conversation only finds X; the tool asks the session to end once his
    short answer has played (WRAP_UP), so the music starts ~1 s later instead of after
    the 20 s follow-up window. From the phone app (no conversation) it plays at once.
  - reminders and other speech outside a conversation: hold() pauses and resumes.
The player/card state comes from librespot's --onevent hook (tools/spotify_event.sh),
so checking it costs no network call.

Dev-mode limits (Feb 2026): the app owner needs Premium, max 5 users, search returns at
most 10 results. Player, search, /me and /me/playlists still work.
"""
from __future__ import annotations

import base64
import difflib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("willie.spotify")

LABEL = "Spotify - music on his own speaker (librespot + Web API)"

DEVICE_NAME = "WILL-E"          # librespot --name in systemd/willie-spotify.service
REPO = Path(__file__).resolve().parents[2]
ENV_FILE = REPO / ".env"
STATE_DIR = REPO / ".local"
API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
TIMEOUT_S = 6.0
CARD_FREE_WAIT_S = 2.5          # after a pause, how long to wait for librespot to let go
SEARCH_LIMIT = 5                # dev mode: max 10

SOORTEN = ["nummer", "artiest", "album", "playlist", "mijn_playlist", "favorieten"]

DECLARATIONS = [
    {
        "name": "speel_muziek",
        "description": (
            "Speel muziek van Wouters Spotify op je eigen speaker. Gebruik dit als hij vraagt om "
            "muziek, een nummer, artiest, album of playlist. Kies de soort: 'nummer' voor een "
            "specifiek nummer (zet de artiest erbij in 'wat' als je die weet), 'artiest' voor 'iets "
            "van X', 'album', 'playlist' voor een sfeer of genre ('rustige jazz', 'focus'), "
            "'mijn_playlist' voor een playlist van Wouter zelf, 'favorieten' voor zijn opgeslagen "
            "nummers. Zonder 'wat' gaat de muziek verder waar hij was. In een gesprek start de "
            "muziek zodra je uitgepraat bent en stopt het gesprek: zeg dus alleen kort wat je "
            "opzet (een halve zin), stel geen vraag terug."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "wat": {"type": "string", "description": "Zoekterm: titel, artiest, album of sfeer. Leeg = hervatten."},
                "soort": {"type": "string", "enum": SOORTEN, "description": "Wat voor iets er gezocht wordt."},
            },
        },
    },
    {
        "name": "muziek_bediening",
        "description": (
            "Bedien de muziek op je speaker: 'pauze' (stop/pauzeer de muziek), 'volgende' of "
            "'vorige' nummer, 'hervat'. Tijdens een gesprek staat de muziek al stil; 'pauze' "
            "betekent dan: niet meer hervatten na het gesprek."
        ),
        "parameters": {
            "type": "object",
            "properties": {"actie": {"type": "string", "enum": ["pauze", "hervat", "volgende", "vorige"]}},
            "required": ["actie"],
        },
    },
    {
        "name": "muziek_volume",
        "description": (
            "Zet het volume van de muziek (niet van je stem) in procent, 0-100. 'Harder' of "
            "'zachter': vraag eerst wat_speelt_er voor het huidige volume en ga er ~15 van af of bij."
        ),
        "parameters": {
            "type": "object",
            "properties": {"procent": {"type": "integer", "description": "0-100"}},
            "required": ["procent"],
        },
    },
    {
        "name": "wat_speelt_er",
        "description": "Welk nummer er op Spotify speelt of het laatst speelde, van wie, en het muziekvolume.",
        "parameters": {"type": "object", "properties": {}},
    },
]


class SpotifyError(RuntimeError):
    """A failure with a short Dutch sentence he can say."""


# ------------------------------------------------------------------ local state (no network)

def _state(name: str) -> str:
    try:
        return (STATE_DIR / f"spotify.{name}").read_text().strip()
    except OSError:
        return ""


def card_busy() -> bool:
    """librespot has the sound card open (it plays, or has not let go yet)."""
    return _state("sink") == "running"


def playing() -> bool:
    return _state("player") == "playing" or card_busy()


def configured() -> bool:
    return all(os.environ.get(k) for k in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REFRESH_TOKEN"))


def _wait_card_free(limit: float = CARD_FREE_WAIT_S) -> bool:
    end = time.monotonic() + limit
    while card_busy() and time.monotonic() < end:
        time.sleep(0.05)
    return not card_busy()


# ------------------------------------------------------------------ Web API

_lock = threading.Lock()
_token = {"value": "", "until": 0.0}
_device = {"id": "", "at": 0.0}


def save_env(key: str, value: str, path: Path = ENV_FILE) -> None:
    """Set one KEY=value line in .env (written by the login and on a rotated refresh token)."""
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [line for line in lines if not line.startswith(f"{key}=")] + [f"{key}={value}"]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    os.environ[key] = value


def token_request(form: dict) -> dict:
    """POST to the accounts service with the app's id + secret (login and refresh)."""
    basic = base64.b64encode(
        f"{os.environ['SPOTIFY_CLIENT_ID']}:{os.environ['SPOTIFY_CLIENT_SECRET']}".encode()).decode()
    request = urllib.request.Request(
        TOKEN_URL, data=urllib.parse.urlencode(form).encode(), method="POST",
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        log.warning("spotify token HTTP %s: %s", exc.code, detail)
        raise SpotifyError("Spotify-login werkt niet meer; draai 'make spotify-login' opnieuw.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SpotifyError("Spotify is niet bereikbaar (internet?).") from exc


def _access_token() -> str:
    if not configured():
        raise SpotifyError("Spotify is nog niet gekoppeld: 'make spotify-login' op de Mac.")
    with _lock:
        if _token["value"] and time.monotonic() < _token["until"]:
            return _token["value"]
        reply = token_request({"grant_type": "refresh_token",
                               "refresh_token": os.environ["SPOTIFY_REFRESH_TOKEN"]})
        if reply.get("refresh_token") and reply["refresh_token"] != os.environ["SPOTIFY_REFRESH_TOKEN"]:
            save_env("SPOTIFY_REFRESH_TOKEN", reply["refresh_token"])
        _token["value"] = reply["access_token"]
        _token["until"] = time.monotonic() + float(reply.get("expires_in", 3600)) - 60
        return _token["value"]


def _api(method: str, path: str, query: dict | None = None, body: dict | None = None,
         retry: bool = True) -> dict | None:
    url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
    data = json.dumps(body).encode() if body is not None else (b"" if method in ("PUT", "POST") else None)
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            raw = response.read()
            try:
                return json.loads(raw) if raw.strip() else None
            except ValueError:              # pause/play answer 200 with a non-JSON body
                return None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        log.warning("spotify %s %s -> HTTP %s: %s", method, path, exc.code, detail)
        if exc.code == 401 and retry:
            _token["value"] = ""
            return _api(method, path, query, body, retry=False)
        if exc.code == 404 and "device" in detail.lower():
            _device["id"] = ""
            raise SpotifyError(f"{DEVICE_NAME} is geen actief Spotify-apparaat; kies hem een keer in de Spotify-app.") from exc
        if exc.code == 403:
            if "restriction" in detail.lower():
                return None                  # e.g. pause while already paused: nothing to do
            raise SpotifyError("Spotify weigert dit (Premium nodig, of dit account staat niet in de app-gebruikers).") from exc
        if exc.code == 429:
            raise SpotifyError("Spotify vindt het even te veel; probeer het zo nog eens.") from exc
        raise SpotifyError(f"Spotify gaf fout {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SpotifyError("Spotify is niet bereikbaar (internet?).") from exc


def _device_id() -> str:
    if _device["id"] and time.monotonic() - _device["at"] < 300:
        return _device["id"]
    devices = (_api("GET", "/me/player/devices") or {}).get("devices", [])
    for device in devices:
        if device.get("name", "").lower() == DEVICE_NAME.lower() and device.get("id"):
            _device.update(id=device["id"], at=time.monotonic())
            return device["id"]
    raise SpotifyError(f"Spotify ziet {DEVICE_NAME} niet als apparaat. Draait willie-spotify, en is "
                       f"{DEVICE_NAME} een keer gekozen in de Spotify-app?")


def _play(body: dict | None = None) -> None:
    _api("PUT", "/me/player/play", {"device_id": _device_id()}, body if body else None)


def _pause() -> None:
    _api("PUT", "/me/player/pause", {"device_id": _device_id()})


# ------------------------------------------------------------------ talking and music take turns

TALKING = False            # a conversation holds the speaker
_resume = False            # music was playing when the conversation started
_pending = None            # (description, callable): what to start when the conversation ends


def _wrap_up() -> None:
    """Ask the live session to end once his short answer has played."""
    from willie.voice import tools as voice_tools
    voice_tools.WRAP_UP.set()


def pause_for_talk() -> None:
    """Wake word heard: pause WILL-E's music and wait until the card is free. Never raises."""
    global TALKING, _resume, _pending
    TALKING, _resume, _pending = True, False, None
    if not configured() or not playing():
        return
    try:
        _pause()
        _resume = True
    except SpotifyError as exc:
        log.warning("pause for talk: %s", exc)
    if not _wait_card_free():
        log.warning("librespot still holds the sound card %.1f s after the pause", CARD_FREE_WAIT_S)


def after_talk(resume: bool = True) -> None:
    """Conversation over: start what was asked, else resume what was playing. Never raises."""
    global TALKING, _resume, _pending
    action, was_playing = _pending, _resume
    TALKING, _resume, _pending = False, False, None
    try:
        if action:
            log.info("music after talk: %s", action[0])
            action[1]()
        elif was_playing and resume:
            _play()
    except SpotifyError as exc:
        log.warning("music after talk: %s", exc)


@contextmanager
def hold():
    """Around speech outside a conversation (reminders, "say" from the phone, chimes):
    pause the music, give the card to his voice, resume afterwards."""
    if TALKING or not configured() or not playing():
        yield
        return
    paused = False
    try:
        _pause()
        paused = True
        _wait_card_free()
    except SpotifyError as exc:
        log.warning("hold: %s", exc)
    try:
        yield
    finally:
        if paused:
            try:
                _play()
            except SpotifyError as exc:
                log.warning("hold resume: %s", exc)


def stop() -> None:
    """Sleep mode: music off, and it does not come back after the conversation."""
    global _resume, _pending
    _resume, _pending = False, None
    if configured() and playing() and not TALKING:
        try:
            _pause()
        except SpotifyError as exc:
            log.warning("stop: %s", exc)


def _do(description: str, action) -> dict:
    """Run a player action now, or - in a conversation - once it has ended."""
    global _pending, _resume
    if TALKING:
        _pending, _resume = (description, action), False
        _wrap_up()
        return {"ok": True, "start": "zodra je uitgepraat bent (het gesprek stopt dan)"}
    action()
    return {"ok": True, "start": "nu"}


# ------------------------------------------------------------------ search

def _names(item: dict) -> str:
    return ", ".join(a.get("name", "") for a in item.get("artists", []))


def _closest(query: str, items: list[dict]) -> dict | None:
    items = [i for i in items if i]
    if not items:
        return None
    q = query.lower()
    return max(items, key=lambda i: difflib.SequenceMatcher(None, q, i.get("name", "").lower()).ratio())


def _find(wat: str, soort: str) -> tuple[str, dict]:
    """(what he can say, body for /me/player/play)."""
    if soort == "favorieten":
        me = _api("GET", "/me") or {}
        return "je favoriete nummers", {"context_uri": f"spotify:user:{me.get('id', '')}:collection"}
    if soort == "mijn_playlist":
        mine = (_api("GET", "/me/playlists", {"limit": 50}) or {}).get("items", [])
        best = _closest(wat, mine)
        if best and difflib.SequenceMatcher(None, wat.lower(), best["name"].lower()).ratio() >= 0.5:
            return f"je playlist {best['name']}", {"context_uri": best["uri"]}
        soort = "playlist"                  # not one of his: search all playlists instead
    kind = {"nummer": "track", "artiest": "artist", "album": "album", "playlist": "playlist"}[soort]
    found = (_api("GET", "/search", {"q": wat, "type": kind, "limit": SEARCH_LIMIT}) or {})
    items = [i for i in found.get(kind + "s", {}).get("items", []) if i]
    if not items:
        raise SpotifyError(f"Niets gevonden op Spotify voor '{wat}'.")
    if kind == "track":
        top = items[0]                      # Spotify's own ranking beats name matching here
        # In its album, so the music goes on after this one song.
        return (f"{top['name']} van {_names(top)}",
                {"context_uri": top["album"]["uri"], "offset": {"uri": top["uri"]}})
    if kind == "artist":
        top = _closest(wat, items[:3]) or items[0]
        return f"muziek van {top['name']}", {"context_uri": top["uri"]}
    if kind == "album":
        top = items[0]
        return f"het album {top['name']} van {_names(top)}", {"context_uri": top["uri"]}
    top = items[0]
    return f"de playlist {top['name']}", {"context_uri": top["uri"]}


# ------------------------------------------------------------------ tools

def speel_muziek(wat: str = "", soort: str = "nummer") -> dict:
    try:
        wat = (wat or "").strip()
        soort = soort if soort in SOORTEN else "nummer"
        if not wat and soort != "favorieten":
            _device_id()
            return {**_do("hervatten", _play), "speelt": "verder waar het was"}
        said, body = _find(wat, soort)
        _device_id()                        # fail now, while he can still say so
        return {**_do(said, lambda: _play(body)), "speelt": said}
    except SpotifyError as exc:
        return {"fout": str(exc)}


def muziek_bediening(actie: str) -> dict:
    global _resume, _pending
    try:
        if actie == "pauze":
            if TALKING:
                _resume, _pending = False, None
                return {"ok": True, "muziek": "blijft uit na het gesprek"}
            _pause()
            return {"ok": True, "muziek": "gepauzeerd"}
        if actie == "hervat":
            return {**_do("hervatten", _play), "muziek": "gaat verder"}
        if actie in ("volgende", "vorige"):
            path = "/me/player/next" if actie == "volgende" else "/me/player/previous"
            device = _device_id()

            def skip():
                _api("POST", path, {"device_id": device})
                if TALKING is False and not playing():
                    _play()                 # skipping a paused player should also play
            return {**_do(f"{actie} nummer", skip), "muziek": f"{actie} nummer"}
        return {"fout": f"onbekende actie {actie}"}
    except SpotifyError as exc:
        return {"fout": str(exc)}


def muziek_volume(procent: int) -> dict:
    try:
        procent = max(0, min(100, int(procent)))
        _api("PUT", "/me/player/volume", {"volume_percent": procent, "device_id": _device_id()})
        return {"ok": True, "muziekvolume": procent}
    except (SpotifyError, ValueError) as exc:
        return {"fout": str(exc)}


def wat_speelt_er() -> dict:
    try:
        player = _api("GET", "/me/player") or {}
    except SpotifyError as exc:
        return {"fout": str(exc)}
    item = player.get("item") or {}
    if not item:
        return {"speelt": "niets"}
    device = player.get("device") or {}
    return {
        "nummer": item.get("name"),
        "artiest": _names(item) or (item.get("show") or {}).get("name"),
        "album": (item.get("album") or {}).get("name"),
        "speelt_nu": bool(player.get("is_playing")),
        "gepauzeerd_voor_gesprek": TALKING and _resume,
        "apparaat": device.get("name"),
        "muziekvolume": device.get("volume_percent"),
    }


HANDLERS = {
    "speel_muziek": speel_muziek,
    "muziek_bediening": muziek_bediening,
    "muziek_volume": muziek_volume,
    "wat_speelt_er": wat_speelt_er,
}


def card() -> dict:
    if not configured():
        return {"spotify": "niet gekoppeld (make spotify-login)"}
    return {"speler": _state("player") or "nog niets gespeeld", "geluidskaart": _state("sink") or "vrij"}
