#!/usr/bin/env python3
"""Read one queued improvement request on stdin, print task/plan/risk as 3 lines."""
import json
import sys

try:
    entry = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(1)
print(entry.get("opdracht", ""))
print(" ".join(entry.get("plan", "").split()))
print(entry.get("risico", "laag"))
