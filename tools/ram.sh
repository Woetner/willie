#!/usr/bin/env bash
# WILL-E RAM harness (A8, D21). Prints a per-process RSS table + one summary line,
# and appends the summary line to ~/.local/share/willie/ram.log.
#   bash tools/ram.sh            table + summary
#   bash tools/ram.sh --summary  summary only
#   bash tools/ram.sh -n 25      show 25 processes (default 15)
# "used" = MemTotal - MemAvailable (what the budget in §5.5 counts).
# "os"   = used - willie core - dashboard - harness  (A8: measured 106-117 MB, budget <= 125).
#          "harness" is this script's own python: it is measuring overhead, not OS.
set -euo pipefail

N=15; SUMMARY_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --summary) SUMMARY_ONLY=1 ;;
    -n) N="$2"; shift ;;
  esac
  shift
done

LOG_DIR="${WILLIE_DATA:-$HOME/.local/share/willie}"
mkdir -p "$LOG_DIR"

python3 - "$N" "$SUMMARY_ONLY" "$LOG_DIR/ram.log" <<'PY'
import os, sys, time
n, summary_only, log_path = int(sys.argv[1]), sys.argv[2] == "1", sys.argv[3]

def meminfo():
    out = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            out[k] = int(v.split()[0]) / 1024  # MB
    return out

procs = []
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        with open(f"/proc/{pid}/status") as f:
            st = dict(l.split(":", 1) for l in f if ":" in l)
        rss = int(st.get("VmRSS", "0 kB").split()[0]) / 1024
        if rss == 0:
            continue  # kernel threads
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
        name = st["Name"].strip()
    except (OSError, KeyError, ValueError):
        continue
    role = ""
    if " -m willie.dashboard" in cmd:
        role = "dashboard"
    elif " -m willie" in cmd:
        role = "core"
    elif int(pid) in (os.getpid(), os.getppid()):
        role = "harness"  # this script measuring itself - not OS overhead
    procs.append((rss, int(pid), name, role, cmd))

procs.sort(reverse=True)
m = meminfo()
used = m["MemTotal"] - m["MemAvailable"]
core = sum(p[0] for p in procs if p[3] == "core")
dash = sum(p[0] for p in procs if p[3] == "dashboard")
harness = sum(p[0] for p in procs if p[3] == "harness")
os_mb = used - core - dash - harness
swap = m.get("SwapTotal", 0) - m.get("SwapFree", 0)

if not summary_only:
    print(f"{'RSS MB':>7}  {'PID':>6}  {'NAME':<16} {'ROLE':<9} CMD")
    for rss, pid, name, role, cmd in procs[:n]:
        print(f"{rss:7.1f}  {pid:6d}  {name:<16} {role:<9} {cmd[:60]}")
    print(f"{sum(p[0] for p in procs):7.1f}  (sum of RSS, {len(procs)} processes; shared pages counted twice)")
    print()

line = (f"RAM {time.strftime('%Y-%m-%d %H:%M')}  total {m['MemTotal']:.0f}  used {used:.0f}  "
        f"avail {m['MemAvailable']:.0f}  os {os_mb:.0f}  core {core:.0f}  dash {dash:.0f}  "
        f"harness {harness:.0f}  swap {swap:.0f}  (MB)")
print(line)
with open(log_path, "a") as f:
    f.write(line + "\n")
PY
