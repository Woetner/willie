#!/usr/bin/env bash
# B6 bench test: MAX98357A + 4 ohm speaker on I2S (GPIO18 BCLK, 19 LRC, 21 DIN, 16 SD).
# Done when: a WAV plays cleanly at a comfortable volume, no hiss when silent.
# Run on the Pi:  bash tools/bench/audio_out.sh     (or `make bench-audio-out`)
set -u
CFG=/boot/firmware/config.txt
echo "== B6 audio out test =="

echo "-- 1. overlay"
if grep -q '^dtoverlay=googlevoicehat-soundcard' "$CFG"; then echo "  PASS  overlay in config.txt"
else echo "  FAIL  add dtoverlay=googlevoicehat-soundcard to $CFG and reboot"; exit 1; fi

echo "-- 2. sound card"
CARD=$(aplay -l 2>/dev/null | awk -F'[ :]' '/^card/ && /voicehat|Voice/ {print $2; exit}')
if [ -n "$CARD" ]; then echo "  PASS  card $CARD: $(aplay -l | grep "^card $CARD" | head -1)"
else echo "  FAIL  no voicehat card:"; aplay -l 2>&1 | sed 's/^/        /'; dmesg | grep -iE 'voicehat|i2s|snd' | tail -5 | sed 's/^/        /'; exit 1; fi
DEV="plughw:$CARD,0"

echo "-- 3. sine 440 Hz, 2 s per channel (listen: clean tone, no crackle)"
speaker-test -D "$DEV" -c 2 -t sine -f 440 -l 1 -p 2000000 >/dev/null 2>&1 \
  && echo "  done" || echo "  FAIL  speaker-test error"

echo "-- 4. speech WAV"
WAV=/usr/share/sounds/alsa/Front_Center.wav
[ -f "$WAV" ] || WAV=$(ls /usr/share/sounds/alsa/*.wav 2>/dev/null | head -1)
if [ -n "$WAV" ]; then aplay -q -D "$DEV" "$WAV" && echo "  played $WAV"
else echo "  no test WAV found (alsa-utils sounds missing)"; fi

echo "-- 5. silence: listen for 5 s with your ear close to the speaker"
sleep 5
echo "== Judge by ear: 1) tone + voice clean?  2) volume comfortable?  3) silent = no hiss? =="
