"""Web search for the live conversation, on the free tier (21 Sep).

On a free-tier key the Gemini 3.x models refuse Google Search grounding (quota 0, Live and
text alike) and the 2.5 text models are closed to new users. The one free route that
searches is the 2.5 native-audio *Live* model. So gemini-3.8-live keeps the conversation
and calls the `zoek_op` tool; this opens a short second Live session with 2.5 + Google
Search, sends the question as text and keeps only the transcript of its (unplayed) spoken
answer.

Cost: nothing, but 11-17 s per search (measured on the Pi) because 2.5 speaks its answer
before the transcript is complete. The paid tier removes the detour: `voice.search: true`
gives 3.8 Google Search directly (J3).
"""
from __future__ import annotations

import asyncio
import json
import os

import websockets

HOST = "generativelanguage.googleapis.com"
PATH = "/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
MODEL = "models/gemini-2.5-flash-native-audio-latest"
INSTRUCTION = ("Zoek het antwoord op met Google Search. Antwoord in 1-3 korte zinnen, "
               "alleen de feiten met datum of bron als dat ertoe doet, in het Nederlands.")


async def search(question: str, api_key: str | None = None, timeout: float = 30.0, url: str | None = None) -> dict:
    """{"antwoord": ..., "gezocht": bool} or {"fout": ...}; never raises."""
    question = question.strip()
    if not question:
        return {"fout": "geen vraag"}
    key = api_key or os.environ.get("GEMINI_API_KEY", "")
    try:
        return await asyncio.wait_for(_ask(question, key, url), timeout)
    except asyncio.TimeoutError:
        return {"fout": f"zoeken duurde langer dan {timeout:.0f} s"}
    except (websockets.WebSocketException, OSError, ValueError, KeyError) as exc:
        return {"fout": f"zoeken mislukt: {exc}"}


async def _ask(question: str, key: str, url: str | None) -> dict:
    async with websockets.connect(url or f"wss://{HOST}{PATH}?key={key}", max_size=None) as ws:
        await ws.send(json.dumps({"setup": {
            "model": MODEL,
            "generationConfig": {"responseModalities": ["AUDIO"]},
            "outputAudioTranscription": {},
            "systemInstruction": {"parts": [{"text": INSTRUCTION}]},
            "tools": [{"googleSearch": {}}],
        }}))
        ack = json.loads(await ws.recv())
        if "setupComplete" not in ack and "setup_complete" not in ack:
            return {"fout": f"zoekmodel weigerde: {json.dumps(ack)[:120]}"}
        await ws.send(json.dumps({"clientContent": {
            "turns": [{"role": "user", "parts": [{"text": question}]}], "turnComplete": True}}))
        text, grounded = [], False
        async for raw in ws:
            content = json.loads(raw).get("serverContent") or {}
            grounded |= "groundingMetadata" in content
            piece = (content.get("outputTranscription") or {}).get("text")
            if piece:
                text.append(piece)
            if content.get("turnComplete"):
                break
    answer = "".join(text).strip()
    return {"antwoord": answer, "gezocht": grounded} if answer else {"fout": "geen antwoord gevonden"}
