#!/usr/bin/env bash
# Installs willie.service for the user that owns the repo. Run with sudo.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
U=$(stat -c %U "$REPO")
H=$(getent passwd "$U" | cut -d: -f6)
[ "$REPO" = "$H/willie" ] || { echo "repo must be at $H/willie (is $REPO)"; exit 1; }

sed -e "s#@USER@#$U#g" -e "s#@HOME@#$H#g" "$REPO/systemd/willie.service" > /etc/systemd/system/willie.service

# let `make deploy` restart the service over ssh without a password prompt
echo "$U ALL=(root) NOPASSWD: /usr/bin/systemctl restart willie, /usr/bin/systemctl stop willie, /usr/bin/systemctl start willie" \
  > /etc/sudoers.d/willie
chmod 440 /etc/sudoers.d/willie
visudo -cf /etc/sudoers.d/willie >/dev/null

systemctl daemon-reload
systemctl enable willie
systemctl restart willie
echo "willie.service installed for $U"
