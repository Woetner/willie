#!/usr/bin/env bash
# Installs willie.service + the on-demand dashboard socket for the user that owns the repo. Run with sudo.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
U=$(stat -c %U "$REPO")
H=$(getent passwd "$U" | cut -d: -f6)
[ "$REPO" = "$H/willie" ] || { echo "repo must be at $H/willie (is $REPO)"; exit 1; }

for unit in willie.service willie-voice.service willie-dashboard.service willie-dashboard.socket; do
  sed -e "s#@USER@#$U#g" -e "s#@HOME@#$H#g" "$REPO/systemd/$unit" > "/etc/systemd/system/$unit"
done

# let `make deploy` restart services over ssh without a password prompt
cat > /etc/sudoers.d/willie <<SUDO
$U ALL=(root) NOPASSWD: /usr/bin/systemctl restart willie, /usr/bin/systemctl stop willie, /usr/bin/systemctl start willie, /usr/bin/systemctl stop willie-dashboard, /usr/bin/systemctl restart willie-dashboard.socket, /usr/bin/systemctl stop willie-voice, /usr/bin/systemctl start willie-voice, /usr/bin/systemctl restart willie-voice
SUDO
chmod 440 /etc/sudoers.d/willie
visudo -cf /etc/sudoers.d/willie >/dev/null

systemctl daemon-reload
systemctl enable willie willie-voice willie-dashboard.socket
systemctl stop willie-dashboard.service 2>/dev/null || true
systemctl restart willie                    # first: an old core may still hold port 8080
systemctl restart willie-dashboard.socket
systemctl restart willie-voice              # hands-free: wake word -> conversation
echo "willie.service + willie-voice.service + willie-dashboard.socket installed for $U"
