#!/bin/bash
# Copy the files worth keeping off an old WILL-E SD card before it is reflashed.
# Run on the Mac with the card inserted (the ext4 partition is not mounted by macOS):
#   sudo bash tools/rescue_sd.sh /dev/disk4s2
# Find the partition with `diskutil list` (the ~30 GB "Linux" one). Read-only: nothing is written to the card.
# Nothing is printed from inside the files (.env holds API keys); only names and sizes.
set -u

DEV="${1:?usage: sudo bash rescue_sd.sh /dev/diskNs2}"
WHO="${SUDO_USER:-$USER}"
OUT="/Users/$WHO/willie-rescue"
DEBUGFS=/opt/homebrew/opt/e2fsprogs/sbin/debugfs
PIHOME=/home/woetner

[ -x "$DEBUGFS" ] || { echo "debugfs not found at $DEBUGFS (brew install e2fsprogs)"; exit 1; }
mkdir -p "$OUT/local" "$OUT/config" "$OUT/share"

echo "from $DEV into $OUT"
"$DEBUGFS" -c -R "dump $PIHOME/willie/.env $OUT/env" "$DEV" 2>&1 | grep -v -i '^debugfs ' || true
"$DEBUGFS" -c -R "rdump $PIHOME/willie/.local $OUT/local" "$DEV" 2>&1 | grep -v -i '^debugfs ' || true
"$DEBUGFS" -c -R "rdump $PIHOME/.config/willie $OUT/config" "$DEV" 2>&1 | grep -v -i '^debugfs ' || true
"$DEBUGFS" -c -R "rdump $PIHOME/.local/share/willie $OUT/share" "$DEV" 2>&1 | grep -v -i '^debugfs ' || true

chown -R "$WHO" "$OUT"
chmod -R go-rwx "$OUT"

echo
echo "rescued (names and sizes only):"
find "$OUT" -type f -exec ls -l {} \; | awk '{print $5, $9}' | sed "s#$OUT/##"
[ -s "$OUT/env" ] && echo "OK: .env rescued" || echo "WARNING: .env is missing or empty"
