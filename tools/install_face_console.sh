#!/usr/bin/env bash
# Installs the on-demand physical face console as the short `willie` command.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$REPO/.venv/bin/python"
CONSOLE="$REPO/tools/willie_console.py"

[ -x "$PYTHON" ] || { echo "missing venv: $PYTHON" >&2; exit 1; }
[ -f "$CONSOLE" ] || { echo "missing face console: $CONSOLE" >&2; exit 1; }

sudo tee /usr/local/bin/willie >/dev/null <<EOF
#!/usr/bin/env sh
exec "$PYTHON" "$CONSOLE" "\$@"
EOF
sudo chmod 755 /usr/local/bin/willie
echo "Installed. On the Pi keyboard, type: willie"
