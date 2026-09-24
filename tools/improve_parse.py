#!/usr/bin/env python3
"""Read one queued improvement request on stdin, print 5 lines: task, plan, risk, id, and
the fingerprint of task/plan/risk (the same one the robot and the hub compute, so the
worker can check that Wouter approved exactly this text in the app)."""
import hashlib
import json
import sys

try:
    entry = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(1)
task, plan, risk = entry.get("opdracht", ""), entry.get("plan", ""), entry.get("risico", "laag")
print(" ".join(task.split()))
print(" ".join(plan.split()))
print(risk)
print("".join(c for c in str(entry.get("id", "")) if c.isalnum()))
print(hashlib.sha256(json.dumps([task, plan, risk], ensure_ascii=False).encode()).hexdigest())
