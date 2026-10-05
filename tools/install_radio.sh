#!/usr/bin/env bash
# Radio on WILL-E (U5b): mpv plays internet streams into the willie_music ALSA device.
# Run with sudo (make radio-setup). Safe to run again. The ALSA device comes from
# install_spotify.sh (config/asound.conf); it is installed here too, so radio also works alone.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
command -v mpv >/dev/null || { apt-get update -qq; apt-get install -y -qq --no-install-recommends mpv; }
install -m 644 "$REPO/config/asound.conf" /etc/asound.conf
U=$(stat -c %U "$REPO")
id willie >/dev/null 2>&1 || bash "$REPO/tools/service_user.sh"
install -d -o "$U" -g willie -m 2770 "$REPO/.local"
mpv --version | head -n 1
echo "radio ready: say 'zet de radio aan' (stations: setting music.stations)"
