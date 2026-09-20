#!/usr/bin/env bash
# Run on the MAC once. Keeps the improvement worker running in the background,
# restarted at login, so Wouter never has to open a terminal on the laptop:
# he approves by voice on the robot and this picks the job up.
#
#   bash tools/install_improve_worker.sh            # install and start
#   bash tools/install_improve_worker.sh --remove   # stop and remove
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="local.willie.improve"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$REPO/.local/improve_worker.log"

if [ "${1:-}" = "--remove" ]; then
  launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed"
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$REPO/.local"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$REPO/tools/improve_worker.sh</string>
    <string>--watch</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict>
</plist>
PLISTEOF

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"
echo "installed and running. log: $LOG"
echo "stop with: bash tools/install_improve_worker.sh --remove"
