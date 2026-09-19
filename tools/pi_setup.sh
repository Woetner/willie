#!/usr/bin/env bash
# WILL-E — step A4 base config. Run ON THE PI as your normal user (uses sudo):
#   bash ~/willie/tools/pi_setup.sh        (or from the Mac: make setup)
# Safe to run again: every step checks first.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CFG=/boot/firmware/config.txt
[ -f "$CFG" ] || CFG=/boot/config.txt
say() { printf '\n\033[1;33m== %s\033[0m\n' "$*"; }

say "1/8 apt update + full-upgrade (takes a while on a Pi 3 A+)"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get -y full-upgrade

say "2/8 packages"
sudo DEBIAN_FRONTEND=noninteractive apt-get -y install \
  git rsync python3-venv python3-dev i2c-tools mosquitto mosquitto-clients \
  build-essential wget unzip
command -v rpicam-hello >/dev/null || sudo apt-get -y install rpicam-apps-lite

say "3/8 interfaces: I2C on, SPI on, serial console + UART off (GPIO14/15 = encoders)"
sudo raspi-config nonint do_i2c 0
sudo raspi-config nonint do_spi 0
sudo raspi-config nonint do_serial_cons 1
sudo raspi-config nonint do_serial_hw 1

say "4/8 $CFG (WILL-E block, §5.2)"
sudo sed -i 's/^dtparam=audio=on/#dtparam=audio=on  # WILL-E: on-board audio off/' "$CFG"
if ! grep -q '>>> WILL-E' "$CFG"; then
  sudo tee -a "$CFG" >/dev/null <<'EOF'

[all]
# >>> WILL-E (tools/pi_setup.sh) >>>
dtparam=audio=off
dtparam=i2c_arm=on
dtparam=spi=on
enable_uart=0
camera_auto_detect=1
# Screen ILI9486 + XPT2046 touch — enabled and tested in step B5:
#dtoverlay=piscreen,speed=16000000,rotate=90
# I2S mics (2x INMP441) + amp (MAX98357A) — chosen and tested in steps B6/B7:
#dtoverlay=...
# <<< WILL-E <<<
EOF
  echo "added WILL-E block"
else
  echo "WILL-E block already present"
fi

say "5/8 pigpio (daemon runs with -t 0 so it does not steal the I2S clock)"
if ! command -v pigpiod >/dev/null; then
  # `apt-cache show` also succeeds for a name that is only referenced (trixie) -> check for a real candidate
  if apt-cache policy pigpio 2>/dev/null | grep -q 'Candidate: [0-9]'; then
    sudo apt-get -y install pigpio python3-pigpio
  else
    echo "pigpio is not packaged for this OS release -> building from source"
    tmp=$(mktemp -d); cd "$tmp"
    wget -q https://github.com/joan2937/pigpio/archive/refs/heads/master.zip -O pigpio.zip
    unzip -q pigpio.zip && cd pigpio-master
    make -j2
    # install ends with `python3 setup.py`, which needs distutils (gone in Python 3.13);
    # the C library + pigpiod are installed before that, and pigpio.py comes from pip in the venv
    sudo make install || command -v pigpiod >/dev/null
    cd / && rm -rf "$tmp"
  fi
fi
sudo ldconfig   # outside the if: a half-finished earlier run may have installed libpigpio without it
PIGPIOD=$(command -v pigpiod)
sed "s#@PIGPIOD@#$PIGPIOD#" "$REPO/systemd/pigpiod.service" | sudo tee /etc/systemd/system/pigpiod.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable pigpiod
sudo systemctl restart pigpiod   # fail here, not silently at the next boot

say "6/8 swap in RAM (zram, 1 GB) for installs"
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

say "7/8 Python venv + requirements"
cd "$REPO"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

say "8/8 willie.service"
sudo bash "$REPO/tools/install_service.sh"

cat <<'EOF'

Done. Now reboot once so config.txt + interfaces take effect:
    sudo reboot
Then check (A4 "done when"):
    i2cdetect -y 1                    # prints a grid (empty is OK before sensors are wired)
    systemctl is-active pigpiod       # active
    rpicam-hello --list-cameras       # lists the camera once it is plugged in
    curl -s localhost:8080/api/status # dashboard answers
EOF
