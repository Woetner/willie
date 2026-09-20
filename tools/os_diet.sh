#!/usr/bin/env bash
# WILL-E — A8 "OS diet" (D20, D21, D22). Run ON THE PI as your normal user (uses sudo):
#   bash ~/willie/tools/os_diet.sh      (or from the Mac: make diet)
# Idempotent. Reboot afterwards, then measure with tools/ram.sh.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CFG=/boot/firmware/config.txt
[ -f "$CFG" ] || CFG=/boot/config.txt
say() { printf '\n\033[1;33m== %s\033[0m\n' "$*"; }
off() {  # stop + disable a unit if it exists
  local u
  for u in "$@"; do
    if systemctl list-unit-files --no-legend "$u" "$u.service" 2>/dev/null | grep -q .; then
      sudo systemctl disable --now "$u" >/dev/null 2>&1 && echo "  off: $u" || true
    fi
  done
}

say "0/6 RAM before"
bash "$REPO/tools/ram.sh" --summary || true

say "1/6 Bluetooth off (overlay + services) and the MCU UART on"
if ! grep -q '^dtoverlay=disable-bt' "$CFG"; then
  sudo sed -i 's/^# <<< WILL-E <<</dtoverlay=disable-bt\n# <<< WILL-E <<</' "$CFG"
  grep -q '^dtoverlay=disable-bt' "$CFG" || printf '[all]\ndtoverlay=disable-bt\n' | sudo tee -a "$CFG" >/dev/null
  echo "  added dtoverlay=disable-bt"
fi
# older A4 blocks had enable_uart=0 (encoders used GPIO14/15); D18 needs the UART for the MCU
sudo sed -i 's/^enable_uart=0/enable_uart=1/' "$CFG"
sudo raspi-config nonint do_serial_cons 1
sudo raspi-config nonint do_serial_hw 0
off hciuart bluetooth

say "2/6 MQTT broker off the Pi (D22: it moves to the house/laptop)"
off mosquitto
if dpkg -s mosquitto >/dev/null 2>&1; then sudo apt-get -y purge mosquitto >/dev/null && echo "  purged mosquitto"; fi

say "3/6 pigpio off (D18: the MCU drives the hardware)"
off pigpiod
if [ -f /etc/systemd/system/pigpiod.service ]; then sudo rm -f /etc/systemd/system/pigpiod.service; echo "  removed pigpiod.service"; fi

say "4/6 other resident services that WILL-E does not need"
# keep: NetworkManager/wpa_supplicant (Wi-Fi), avahi-daemon (willie.local), ssh, cron, timesyncd, getty
off triggerhappy ModemManager udisks2 packagekit rpi-connect rpi-connect-wayvnc
sudo systemctl --global disable rpi-connect rpi-connect-wayvnc >/dev/null 2>&1 || true

say "5/6 journald in RAM with a size cap"
sudo mkdir -p /etc/systemd/journald.conf.d
sudo tee /etc/systemd/journald.conf.d/50-willie.conf >/dev/null <<'EOF'
# WILL-E (A8): logs in RAM, capped. willie also keeps its own 1 MB rotating file.
[Journal]
Storage=volatile
RuntimeMaxUse=16M
RuntimeMaxFileSize=4M
EOF
sudo systemctl restart systemd-journald

say "6/6 service units (MemoryMax + on-demand dashboard)"
sudo bash "$REPO/tools/install_service.sh"
sudo systemctl daemon-reload

cat <<'EOF'

Done. Reboot so the overlay + UART change take effect:
    sudo reboot
After ~2 min idle:
    bash ~/willie/tools/ram.sh          # A8 done when "os" <= 80 MB
    ls -l /dev/serial0                  # -> ttyAMA0
EOF
