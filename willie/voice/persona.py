"""The one place the character comes from: config/persona.md.

Keeping the prompt in a file rather than in code is what D1 asks for - Wouter
edits it and reads it aloud to check it sounds like WILL-E, without touching
Python.
"""

from __future__ import annotations

from pathlib import Path

PERSONA_FILE = Path(__file__).resolve().parents[2] / "config" / "persona.md"

# Used only if the file is missing, so a broken deploy still has a character.
FALLBACK = (
    "Je bent WILL-E, de robot van Wouter. Kort en direct antwoorden, "
    "een of twee zinnen, geen vulling, geen slijmen. Zeg het gewoon als je "
    "iets niet weet. Nederlands."
)


def system_prompt(extra: str = "") -> str:
    text = PERSONA_FILE.read_text(encoding="utf-8") if PERSONA_FILE.exists() else FALLBACK
    return f"{text}\n\n{extra}".strip() if extra else text
