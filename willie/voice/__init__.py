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


def talk_keys() -> list[str]:
    """Free key first, then the paid one: when the free key is refused (limit reached, model
    not on the free tier), talking switches to the paid key by itself (Wouter, 25 Sep)."""
    keys = [os.environ.get("GEMINI_API_KEY_FREE", ""), os.environ.get("GEMINI_API_KEY", "")]
    return [k for k in dict.fromkeys(keys) if k]


# Which key and model the running conversation uses (Wouter, 25 Sep: "can I ask him if he
# runs on the paid key?"). Set by GeminiLiveAdapter; read by the `verbinding` tool.
SESSION = {"model": None, "key": None}
KEY_HOOK = None          # fn(paid: bool): the PAID badge on his face; set by tools/willie_voice.py


def key_kind(key: str) -> str:
    free = os.environ.get("GEMINI_API_KEY_FREE", "")
    return "gratis" if free and key == free else "betaald"
