"""Token meter (J3, 25 Sep): what every Gemini call costs, so the cost of talking is a number.

Measured 25 Sep on gemini-3.8-live: the Live API reports `usageMetadata` once per turn, and
`promptTokenCount` is the *whole* context every time - the system prompt (persona 3.6k
tokens) and all earlier audio are paid again on each turn. So the size of the setup prompt
decides most of the bill.

One hook per process: the robot sends each record to the hub (willie/hub/call
"gebruik_sessie"), the hub stores it (hub/usage.py). No hook set = records are dropped.

Prices are USD per 1M tokens, list prices of 25 Sep. Google has no separate price for the
3.8 Live model on its page, so it uses the 2.5 native-audio prices (the J3 note: $0.005/min
in, $0.018/min out). The hub can override them in ~/homeserver-data/usage.yaml.
"""
from __future__ import annotations

import logging

log = logging.getLogger("willie.usage")

HOOK = None                 # fn(record: dict); set by tools/willie_voice.py (robot) or the hub

# (text in, audio in, image in, text out, audio out); thoughts are billed as text out.
PRICES = {
    "live": (0.50, 3.00, 3.00, 2.00, 12.00),
    "tts": (0.50, 0.50, 0.50, 10.00, 10.00),
    "flash-lite": (0.10, 0.30, 0.10, 0.40, 0.40),
    "flash": (0.30, 1.00, 0.30, 2.50, 2.50),
}


def family(model: str) -> str:
    m = model.lower()
    if "live" in m or "native-audio" in m:
        return "live"
    if "tts" in m:
        return "tts"
    return "flash-lite" if "lite" in m else "flash"


def tokens(meta: dict | None) -> dict:
    """usageMetadata -> flat counts. Modalities not listed count as text."""
    meta = meta or {}
    out = {"prompt_text": 0, "prompt_audio": 0, "prompt_image": 0,
           "out_text": 0, "out_audio": 0, "thoughts": int(meta.get("thoughtsTokenCount") or 0)}
    for field, prefix in (("promptTokensDetails", "prompt_"), ("responseTokensDetails", "out_"),
                          ("candidatesTokensDetails", "out_")):
        for d in meta.get(field) or []:
            kind = {"AUDIO": "audio", "IMAGE": "image", "VIDEO": "image"}.get(d.get("modality"), "text")
            if prefix == "out_" and kind == "image":
                kind = "text"
            out[prefix + kind] += int(d.get("tokenCount") or 0)
    # Some answers only give totals: put what the details miss under text.
    prompt = int(meta.get("promptTokenCount") or 0)
    answer = int(meta.get("responseTokenCount") or meta.get("candidatesTokenCount") or 0)
    out["prompt_text"] += max(0, prompt - out["prompt_text"] - out["prompt_audio"] - out["prompt_image"])
    out["out_text"] += max(0, answer - out["out_text"] - out["out_audio"])
    return out


def add(total: dict, meta: dict | None) -> dict:
    for k, v in tokens(meta).items():
        total[k] = total.get(k, 0) + v
    total["turns"] = total.get("turns", 0) + 1
    return total


def usd(model: str, counts: dict, prices: dict | None = None) -> float:
    p = (prices or PRICES).get(family(model)) or PRICES[family(model)]
    return (counts.get("prompt_text", 0) * p[0] + counts.get("prompt_audio", 0) * p[1]
            + counts.get("prompt_image", 0) * p[2]
            + (counts.get("out_text", 0) + counts.get("thoughts", 0)) * p[3]
            + counts.get("out_audio", 0) * p[4]) / 1e6


def report(source: str, model: str, key: str, counts: dict | None = None, meta: dict | None = None) -> None:
    """One record to the hook: `counts` (summed with add()) or one call's raw `meta`.
    `key` = "gratis" or "betaald" (willie.voice.key_kind). Never raises."""
    try:
        counts = dict(counts) if counts is not None else add({}, meta)
        if not counts.get("turns"):
            return
        record = {"source": source, "model": model.removeprefix("models/"), "key": key, **counts}
        if HOOK:
            HOOK(record)
    except Exception:
        log.exception("usage report failed")
