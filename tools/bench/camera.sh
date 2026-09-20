#!/usr/bin/env bash
# B8 bench test: Camera Module 3 on the long flex cable.
# Done when: 12 MP still, 640x480 stream at 10 fps, autofocus sharp at 20 cm.
# Run on the Pi:  bash tools/bench/camera.sh     (or `make bench-camera` on the Mac)
set -u
OUT="$HOME/bench/b8"; mkdir -p "$OUT"; rm -f "$OUT"/*
pass=0; fail=0
ok()  { echo "  PASS  $*"; pass=$((pass+1)); }
bad() { echo "  FAIL  $*"; fail=$((fail+1)); }
cma() { awk '/CmaFree/{print $2/1024 " MB"}' /proc/meminfo; }

echo "== B8 camera test =="
echo "CMA free before: $(cma)"

echo "-- 1. detect"
if rpicam-hello --list-cameras 2>&1 | tee "$OUT/list.txt" | grep -q imx708; then
  ok "imx708 detected"; else bad "no imx708 (flex cable / contacts facing HDMI?)"; fi

echo "-- 2. 12 MP still (4608x2592)"
if rpicam-still -n -t 2000 --autofocus-on-capture --width 4608 --height 2592 \
     -o "$OUT/full_12mp.jpg" >"$OUT/still.log" 2>&1; then
  ok "12 MP still, $(du -h "$OUT/full_12mp.jpg" | cut -f1)"
else
  bad "12 MP still failed:"; grep -iE "error|fail|dma|cma" "$OUT/still.log" | head -5 | sed 's/^/        /'
fi

echo "-- 3. 640x480 @ 10 fps for 10 s"
if rpicam-vid -n -t 10000 --width 640 --height 480 --framerate 10 \
     --save-pts "$OUT/pts.txt" -o "$OUT/stream.h264" >"$OUT/vid.log" 2>&1; then
  fps=$(awk 'NR>2{n++; last=$1} NR==2{first=$1} END{ if (n>0) printf "%.2f", n*1000/(last-first); else print 0 }' "$OUT/pts.txt")
  frames=$(($(wc -l <"$OUT/pts.txt")-1))
  if awk "BEGIN{exit !($fps >= 9.5)}"; then ok "stream $frames frames, $fps fps"
  else bad "stream only $fps fps ($frames frames)"; fi
else
  bad "rpicam-vid failed:"; tail -3 "$OUT/vid.log" | sed 's/^/        /'
fi

echo "-- 4. autofocus at 20 cm: put a PCB or ruler 20 cm from the lens, then press Enter"
read -r _
rpicam-still -n -t 2000 --autofocus-on-capture -o "$OUT/af_auto.jpg" >/dev/null 2>&1 \
  && ok "AF auto shot saved" || bad "AF auto shot failed"
rpicam-still -n -t 1000 --autofocus-mode manual --lens-position 5 -o "$OUT/af_manual_20cm.jpg" >/dev/null 2>&1 \
  && ok "AF manual 5 dioptre (=20 cm) shot saved" || bad "AF manual shot failed"
echo "        -> judge sharpness by eye on the Mac (af_auto.jpg vs af_manual_20cm.jpg)"

echo "CMA free after: $(cma)"
echo "== $pass passed, $fail failed. Files in $OUT =="
