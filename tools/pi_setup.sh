#!/usr/bin/env bash
# WILL-E — base config for a FRESH Pi (A4, updated for D18/D20/D22).
# Run ON THE PI as your normal user (uses sudo):
#   bash ~/willie/tools/pi_setup.sh        (or from the Mac: make setup)
# Then: bash ~/willie/tools/os_diet.sh     (A8)  and reboot.
# Safe to run again: every step checks first.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CFG=/boot/firmware/config.txt
[ -f "$CFG" ] || CFG=/boot/config.txt
say() { printf '\n\033[1;33m== %s\033[0m\n' "$*"; }

say "1/7 apt update + full-upgrade (takes a while on a Pi 3 A+)"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get -y full-upgrade

say "2/7 packages (no mosquitto on the Pi: D22; no pigpio: D18)"
sudo DEBIAN_FRONTEND=noninteractive apt-get -y install \
  git rsync python3-venv python3-dev i2c-tools
command -v rpicam-hello >/dev/null || sudo apt-get -y install rpicam-apps-lite

say "3/7 interfaces: I2C + SPI on; serial console OFF, UART hardware ON (GPIO14/15 = MCU link, D18)"
sudo raspi-config nonint do_i2c 0
sudo raspi-config nonint do_spi 0
sudo raspi-config nonint do_serial_cons 1
sudo raspi-config nonint do_serial_hw 0

say "4/7 $CFG (WILL-E block, §5.2)"
sudo sed -i 's/^dtparam=audio=on/#dtparam=audio=on  # WILL-E: on-board audio off/' "$CFG"
if ! grep -q '>>> WILL-E' "$CFG"; then
  sudo tee -a "$CFG" >/dev/null <<'EOF'

[all]
# >>> WILL-E (tools/pi_setup.sh) >>>
dtparam=audio=off
dtparam=i2c_arm=on
dtparam=spi=on
# UART on GPIO14/15 to the ESP32 (D18). disable-bt gives the good PL011 UART
# (/dev/serial0 = ttyAMA0, needed for 921600 baud) and switches Bluetooth off (A8).
enable_uart=1
dtoverlay=disable-bt
camera_auto_detect=1
# Screen ILI9486 + XPT2046 touch (B5, measured 20 Sep). The 3.5" board needs the
# tft35a overlay, not piscreen. speed=48000000 is what the SPI block turns into
# 41.7 MHz (250 MHz / 6); fps=150 is the fbtft deferred-io rate — at the default
# 60 its 16.7 ms sleep, not SPI, was the thing capping the blink at 17 fps.
dtoverlay=tft35a:rotate=90,speed=48000000,fps=150,txbuflen=65536
# I2S mics (2x INMP441) + amp (MAX98357A) — chosen and tested in steps B6/B7:
#dtoverlay=...
# <<< WILL-E <<<
EOF
  echo "added WILL-E block"
else
  echo "WILL-E block already present (tools/os_diet.sh migrates older blocks)"
fi

say "5/7 swap in RAM (zram, 1 GB) for installs"
if dpkg -s rpi-swap >/dev/null 2>&1; then
  # Pi OS trixie+ already runs zram swap via rpi-swap (default = RAM size); just raise it. Active after reboot.
  sudo mkdir -p /etc/rpi/swap.conf.d
  printf '[Zram]\nFixedSizeMiB=1024\n' | sudo tee /etc/rpi/swap.conf.d/50-willie.conf >/dev/null
  # zram-tools (older runs of this script) fights rpi-swap over /dev/zram0
  if dpkg -s zram-tools >/dev/null 2>&1; then sudo apt-get -y purge zram-tools; fi
elif ! grep -q zram /proc/swaps; then
  sudo apt-get -y install zram-tools
  printf 'ALGO=zstd\nSIZE=1024\nPRIORITY=100\n' | sudo tee /etc/default/zramswap >/dev/null
  sudo systemctl restart zramswap || true
fi
cat /proc/swaps

say "6/7 Python venv + requirements"
cd "$REPO"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

say "7/7 services (willie + dashboard)"
sudo bash "$REPO/tools/install_service.sh"

cat <<'EOF'

Done. Next:
    bash ~/willie/tools/os_diet.sh     # A8: Bluetooth/MQTT/pigpio off, journald in RAM
    sudo reboot
Then check:
    i2cdetect -y 1                     # prints a grid
    ls -l /dev/serial0                 # -> ttyAMA0 (MCU link)
    rpicam-hello --list-cameras        # lists the camera once it is plugged in
    bash ~/willie/tools/ram.sh         # RAM table (A8)
EOF
