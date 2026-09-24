"""Template for a new skill (H1). Copy to `willie/skills/<name>.py` (no leading "_"),
fill in the four parts below, `make deploy`, and it shows up in the dashboard's Skills
tab. Settings the skill needs go into config/schema.yaml under `skills:` (D13: no
magic numbers in code); secrets go into `.env`, never here.

Rules (§3.1, tools.py): a tool returns a small dict (1-3 kB - it is read by a voice
model), never raises for normal failures (say what went wrong in {"fout": ...}), and
anything that spends money, messages a person or moves the robot needs spoken
confirmation first. If the system lives on the home server, forward the call there
like reminders.py does, and say in one sentence when the server is gone (D22).
"""
from __future__ import annotations

LABEL = "Voorbeeld - short description for the dashboard"

DECLARATIONS = [
    {
        "name": "voorbeeld",
        "description": "Wanneer het model deze tool moet gebruiken, in het Nederlands.",
        "parameters": {
            "type": "object",
            "properties": {
                "vraag": {"type": "string", "description": "Wat Wouter wil weten."},
            },
            "required": ["vraag"],
        },
    },
]


def voorbeeld(vraag: str) -> dict:
    return {"antwoord": f"Voorbeeld: {vraag}"}


HANDLERS = {"voorbeeld": voorbeeld}


def card() -> dict:
    """Optional. A few key/value pairs for the dashboard card; must not block."""
    return {"status": "ok"}
