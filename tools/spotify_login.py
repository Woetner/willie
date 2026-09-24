#!/usr/bin/env python3
"""Link WILL-E to Wouter's Spotify (Web API), once. Run from the Mac: `make spotify-login`.

It runs on the Pi. The Mac's ssh forwards 127.0.0.1:8888 to it, so the Spotify login
page in the Mac's browser can hand its answer back to this script. The client id and
secret are typed here (hidden) and go straight into the Pi's .env, like the refresh
token: nothing secret is printed, and nothing goes into git or a chat.

Before: create an app at https://developer.spotify.com/dashboard (Web API), add the
redirect URI below, and (dev mode) your own Spotify account under User Management.
"""
from __future__ import annotations

import getpass
import http.server
import os
import secrets
import sys
import time
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from ask_camera import load_env  # noqa: E402
from willie.skills import spotify  # noqa: E402

PORT = 8888
REDIRECT = f"http://127.0.0.1:{PORT}/callback"
SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing playlist-read-private user-library-read"


def ask_app() -> None:
    if os.environ.get("SPOTIFY_CLIENT_ID") and os.environ.get("SPOTIFY_CLIENT_SECRET"):
        return
    print("Spotify app (developer.spotify.com/dashboard -> your app -> Settings):")
    spotify.save_env("SPOTIFY_CLIENT_ID", input("  Client ID: ").strip())
    spotify.save_env("SPOTIFY_CLIENT_SECRET", getpass.getpass("  Client secret (hidden): ").strip())


def wait_for_code(state: str) -> str:
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            ok = query.get("state", [""])[0] == state and "code" in query
            if ok:
                result["code"] = query["code"][0]
            else:
                result["error"] = query.get("error", ["wrong state"])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            text = "WILL-E is gekoppeld aan Spotify. Je kunt dit venster sluiten." if ok else \
                f"Koppelen mislukt: {result['error']}"
            self.wfile.write(f"<p style='font:20px sans-serif'>{text}</p>".encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    while not result:
        server.handle_request()
    server.server_close()
    if "code" not in result:
        raise SystemExit(f"Spotify login failed: {result.get('error')}")
    return result["code"]


def main() -> int:
    load_env(REPO / ".env")
    ask_app()
    state = secrets.token_urlsafe(16)
    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": os.environ["SPOTIFY_CLIENT_ID"], "response_type": "code", "redirect_uri": REDIRECT,
        "scope": SCOPES, "state": state})
    print(f"\nRedirect URI in the app settings must be exactly:  {REDIRECT}")
    print("Open this link on the Mac and log in:\n")
    print(url + "\n", flush=True)
    code = wait_for_code(state)
    reply = spotify.token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT})
    spotify.save_env("SPOTIFY_REFRESH_TOKEN", reply["refresh_token"])
    spotify.ROTATED_TOKEN.unlink(missing_ok=True)       # a fresh login beats an old rotated token
    print("Linked: refresh token saved in the Pi's .env.")

    print(f"\nLooking for the speaker '{spotify.DEVICE_NAME}' ... (open the Spotify app on the home"
          f" Wi-Fi, tap the speaker icon, pick {spotify.DEVICE_NAME} once)", flush=True)
    for _ in range(60):
        try:
            spotify._device_id()
            print(f"{spotify.DEVICE_NAME} is a Spotify speaker. Done: say 'Hey Willie, speel ...'.")
            return 0
        except spotify.SpotifyError:
            time.sleep(3)
    print(f"{spotify.DEVICE_NAME} did not show up in 3 min. Check `make spotify-logs`, then run this again.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
