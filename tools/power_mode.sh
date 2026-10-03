#!/bin/sh
# Deep sleep, Pi side (Phase P, D31). Installed root-owned as /usr/local/sbin/willie-power-mode by
# tools/service_user.sh; the core (user willie) may run exactly `sleep` and `wake` through sudo.
#
#   sleep: CPU governor powersave (600 MHz), Wi-Fi power save on, ACT LED off, Spotify stopped
#   wake:  everything back as it was before `sleep` (remembered in /run/willie-power/)
# Wi-Fi power save needs `iw` (tools/pi_setup.sh installs it).
#
# Every step may fail on its own (no iw, no LED, no Spotify unit): the rest still runs.
set -u
STATE=/run/willie-power
mode=${1:-}
case "$mode" in sleep|wake) ;; *) echo "usage: $0 sleep|wake" >&2; exit 2 ;; esac
mkdir -p "$STATE"
LED=/sys/class/leds/ACT/trigger

if [ "$mode" = sleep ]; then
  if [ ! -f "$STATE/asleep" ]; then           # remember the awake values once
    cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor > "$STATE/governor" 2>/dev/null
    sed -n 's/.*\[\(.*\)\].*/\1/p' "$LED" > "$STATE/led" 2>/dev/null
    command -v iw >/dev/null && iw dev wlan0 get power_save 2>/dev/null | awk '{print $NF}' > "$STATE/power_save"
    systemctl is-active --quiet willie-spotify && touch "$STATE/spotify"
    touch "$STATE/asleep"
  fi
  governor=powersave; power_save=on; led=none
  systemctl stop willie-spotify 2>/dev/null
else
  governor=$(cat "$STATE/governor" 2>/dev/null || echo ondemand)
  led=$(cat "$STATE/led" 2>/dev/null || echo mmc0)
  power_save=$(cat "$STATE/power_save" 2>/dev/null || echo off)
  [ -f "$STATE/spotify" ] && systemctl start willie-spotify 2>/dev/null
  rm -f "$STATE/asleep" "$STATE/spotify"
fi

for f in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
  echo "$governor" > "$f" 2>/dev/null
done
command -v iw >/dev/null && iw dev wlan0 set power_save "$power_save" 2>/dev/null
[ -w "$LED" ] && echo "$led" > "$LED" 2>/dev/null
echo "$mode: governor $governor, wifi power_save $power_save, ACT led $led"
exit 0
