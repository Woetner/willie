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

say "0/7 RAM before"
bash "$REPO/tools/ram.sh" --summary || true

say "1/7 Bluetooth off (overlay + services) and the MCU UART on"
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

say "2/7 MQTT broker off the Pi (D22: it moves to the house/laptop)"
off mosquitto
if dpkg -s mosquitto >/dev/null 2>&1; then sudo apt-get -y purge mosquitto >/dev/null && echo "  purged mosquitto"; fi

say "3/7 pigpio off (D18: the MCU drives the hardware)"
off pigpiod
if [ -f /etc/systemd/system/pigpiod.service ]; then sudo rm -f /etc/systemd/system/pigpiod.service; echo "  removed pigpiod.service"; fi

say "4/7 other resident services that WILL-E does not need"
# keep: NetworkManager/wpa_supplicant (Wi-Fi), avahi-daemon (willie.local), ssh, cron, timesyncd, getty
off triggerhappy ModemManager udisks2 packagekit rpi-connect rpi-connect-wayvnc
sudo systemctl --global disable rpi-connect rpi-connect-wayvnc >/dev/null 2>&1 || true
# polkit only authorises non-root D-Bus clients (e.g. nmcli without sudo); headless WILL-E uses sudo.
if systemctl list-unit-files --no-legend polkit.service 2>/dev/null | grep -q .; then
  sudo systemctl mask --now polkit.service >/dev/null 2>&1 && echo "  masked: polkit (use 'sudo nmcli')"
fi
# cloud-init only matters on the first boot (A3 is done); stop it re-running its units at every boot
if [ -d /etc/cloud ] && [ ! -f /etc/cloud/cloud-init.disabled ]; then
  sudo touch /etc/cloud/cloud-init.disabled && echo "  off: cloud-init (after first boot)"
fi

say "5/7 no desktop GPU stack; CMA down to 64 MB (D20, D21)"
# vc4-kms-v3d reserves 256 MB of CMA on a Pi with 415 MB usable, which alone blows the
# A8 budget. WILL-E has no desktop: the face is a direct framebuffer over SPI (D20).
# The camera does need CMA buffers, so keep a 64 MB region instead of dropping it.
for k in dtoverlay=vc4-kms-v3d max_framebuffers=2 display_auto_detect=1 disable_fw_kms_setup=1; do
  sudo sed -i "s|^${k}\$|#willie# ${k}|" "$CFG" && grep -q "^#willie# ${k}" "$CFG" && echo "  off: ${k}"
done
if ! grep -q '^dtoverlay=cma,cma-64' "$CFG"; then
  sudo sed -i '0,/^dtoverlay=disable-bt/s//dtoverlay=disable-bt\ndtoverlay=cma,cma-64/' "$CFG"
  grep -q '^dtoverlay=cma,cma-64' "$CFG" || printf '[all]\ndtoverlay=cma,cma-64\n' | sudo tee -a "$CFG" >/dev/null
  echo "  added dtoverlay=cma,cma-64"
fi

say "6/7 journald in RAM with a size cap"
sudo mkdir -p /etc/systemd/journald.conf.d
sudo tee /etc/systemd/journald.conf.d/50-willie.conf >/dev/null <<'EOF'
# WILL-E (A8): logs in RAM, capped. willie also keeps its own 1 MB rotating file.
[Journal]
Storage=volatile
RuntimeMaxUse=4M
RuntimeMaxFileSize=1M
EOF
sudo systemctl restart systemd-journald

say "7/7 service units (MemoryMax + on-demand dashboard)"
sudo bash "$REPO/tools/install_service.sh"
sudo systemctl daemon-reload

cat <<'EOF'

Done. Reboot so the overlay + UART change take effect:
    sudo reboot
After ~2 min idle:
    bash ~/willie/tools/ram.sh          # A8 done when "os" <= 80 MB
    ls -l /dev/serial0                  # -> ttyAMA0
EOF
