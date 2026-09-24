"""Voice adapters and tools.

Two Gemini keys (Wouter, 24 Sep): GEMINI_API_KEY is the paid key and stays the default for
everything with private content (photos, camera stills, memory merges, the planner, garage
danger checks). Plain talking and searching use GEMINI_API_KEY_FREE, a key from a second
Google project without billing, when it is set. Without it everything uses the paid key.
Note: what is said in a conversation goes over the talk key, including tool results he reads out.
"""
import os


def talk_key() -> str:
    """Key for live conversations, the phone chat, speech and web search."""
    return os.environ.get("GEMINI_API_KEY_FREE") or os.environ.get("GEMINI_API_KEY", "")
