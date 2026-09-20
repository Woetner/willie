#!/usr/bin/env python3
"""Print one result line for the robot to read back: improve_report.py <status> <text>."""
import json
import sys
from datetime import datetime

print(json.dumps(
    {
        "tijd": datetime.now().isoformat(timespec="seconds"),
        "status": sys.argv[1] if len(sys.argv) > 1 else "?",
        "tekst": sys.argv[2] if len(sys.argv) > 2 else "",
    },
    ensure_ascii=False,
))
