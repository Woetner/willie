#!/usr/bin/env bash
# Spotify on WILL-E: librespot (raspotify package) + the willie_music ALSA device +
# willie-spotify.service. Run with sudo (make spotify-setup). Safe to run again.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
U=$(stat -c %U "$REPO")
H=$(getent passwd "$U" | cut -d: -f6)
[ "$REPO" = "$H/willie" ] || { echo "repo must be at $H/willie (is $REPO)"; exit 1; }

if ! command -v librespot >/dev/null; then
  curl -fsSL https://dtcooper.github.io/raspotify/key.asc -o /usr/share/keyrings/raspotify_key.asc
  echo "deb [signed-by=/usr/share/keyrings/raspotify_key.asc] https://dtcooper.github.io/raspotify raspotify main" \
    > /etc/apt/sources.list.d/raspotify.list
  apt-get update -qq
  apt-get install -y -qq raspotify
fi
# Only the librespot binary is used: the package's own service runs as another user
# and could not write the state files the voice loop reads.
systemctl disable --now raspotify 2>/dev/null || true

install -m 644 "$REPO/config/asound.conf" /etc/asound.conf
install -d -o "$U" -g "$U" "$REPO/.local" "$REPO/.local/librespot"
sed -e "s#@USER@#$U#g" -e "s#@HOME@#$H#g" "$REPO/systemd/willie-spotify.service" \
  > /etc/systemd/system/willie-spotify.service

cat > /etc/sudoers.d/willie-spotify <<SUDO
$U ALL=(root) NOPASSWD: /usr/bin/systemctl restart willie-spotify, /usr/bin/systemctl stop willie-spotify, /usr/bin/systemctl start willie-spotify
SUDO
chmod 440 /etc/sudoers.d/willie-spotify
visudo -cf /etc/sudoers.d/willie-spotify >/dev/null

systemctl daemon-reload
systemctl enable willie-spotify
systemctl restart willie-spotify
echo "willie-spotify.service installed: 'WILL-E' should now show up as a speaker in the Spotify app"
