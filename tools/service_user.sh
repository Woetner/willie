#!/usr/bin/env bash
# Security audit (option B, 25 Sep, Wouter): WILL-E's services run as their own user
# `willie`, which has no password, no login and no sudo except power off / reboot. A bug in
# the voice software (it talks to the internet and reads AI output) can then no longer take
# over the Pi. `woetner` stays the deploy account (SSH key only) and joins group `willie`.
#
# The service user may READ the code and the venv, and WRITE only runtime state:
#   ~/willie/.local, ~/.config/willie (settings), ~/.local/share/willie (log, state.json).
# It can never write code that woetner or root later runs.
#
# Run with sudo (tools/install_service.sh does). Safe to run again.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OWNER=$(stat -c %U "$REPO")
HOME_DIR=$(getent passwd "$OWNER" | cut -d: -f6)
SVC=willie

if ! id "$SVC" >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/willie --create-home --shell /usr/sbin/nologin \
    --comment "WILL-E services" "$SVC"
fi
# Hardware and logs: sound, camera + framebuffer + vcgencmd, GPIO/I2C/SPI, touch, UART to the ESP32.
for g in audio video render gpio i2c spi input dialout systemd-journal; do
  getent group "$g" >/dev/null && usermod -aG "$g" "$SVC"
done
usermod -aG "$SVC" "$OWNER"                    # woetner reads/writes the runtime state too

chmod 711 "$HOME_DIR"                          # others may pass through, not list
for dir in "$REPO/.local" "$HOME_DIR/.config/willie" "$HOME_DIR/.local/share/willie"; do
  mkdir -p "$dir"
  chown -R "$OWNER:$SVC" "$dir"
  chmod -R g+rwX,o-rwx "$dir"
  find "$dir" -type d -exec chmod g+s {} +     # new files belong to group willie
done
[ -f "$REPO/.env" ] && chown "$OWNER:$SVC" "$REPO/.env" && chmod 640 "$REPO/.env"

cat > /etc/sudoers.d/willie-service <<SUDO
# The services may only switch the robot off or restart it (zet_uit, battery cut-off).
$SVC ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff, /usr/bin/systemctl reboot
SUDO
chmod 440 /etc/sudoers.d/willie-service
visudo -cf /etc/sudoers.d/willie-service >/dev/null

# The face owns the panel's console (tty1). No auto-login shell there any more: a keyboard
# plugged into the robot gave a logged-in shell with sudo.
rm -f /etc/systemd/system/getty@tty1.service.d/autologin.conf
systemctl disable --now getty@tty1.service 2>/dev/null || true
cat > /etc/udev/rules.d/70-willie-tty1.rules <<'RULE'
# WILL-E's face hides the console cursor on the panel (willie/face/runtime.py Console).
KERNEL=="tty1", GROUP="willie", MODE="0660"
RULE
udevadm control --reload && udevadm trigger --name-match=tty1 || true
chgrp "$SVC" /dev/tty1 && chmod 660 /dev/tty1
echo "service user $SVC ready"
