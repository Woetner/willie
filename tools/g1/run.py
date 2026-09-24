#!/usr/bin/env python3
"""Gate G1 test sheet (D8) for the live voice adapter. Runs on the Pi (it has the key).

For each question in questions.yaml: synthesize it once with Gemini TTS in a *different*
voice than WILL-E's (cached in .local/g1/), open a fresh Live session with the real persona,
live context and tool declarations, stream the question in real time followed by silence,
and measure:

- reply latency = end of the question audio -> first audio back (target < 800 ms)
- which tools were called (handlers are stubs: nothing moves, nothing is stored)
- the spoken answer (output transcription), checked against `expect` words

Personality (1-5) is Wouter's call: the answers are in the report for him to read.

    .venv/bin/python tools/g1/run.py            # all 20
    .venv/bin/python tools/g1/run.py tijd kijk  # a few, by id
    .venv/bin/python tools/g1/run.py --silence 400   # try another silenceDurationMs
"""
from __future__ import annotations

import argparse
import array
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from willie.voice import gemini_live  # noqa: E402
from willie.voice.base import Tool  # noqa: E402
from willie.voice.persona import system_prompt  # noqa: E402
from willie.audio import speech  # noqa: E402

OUT = REPO / ".local" / "g1"
QUESTION_VOICE = "Puck"          # not Iapetus: the model must not hear itself
CHUNK_MS = 100
STUB = {
    "kijk": {"beschrijving": "Een ESP32 DevKit op een breadboard met drie jumperdraden."},
    "gezondheid": {"slaap": {"score": 78, "duur": "7 u 12 min", "diep": "1 u 20 min"}},
    "status": {"cpu_temp_c": 48.3, "ram_vrij_mb": 201},
    "agenda": {"vandaag": [{"tijd": "15:30", "wat": "werk"}]},
}


def load_env():
    env = REPO / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                import os
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def resample_24k_to_16k(pcm: bytes) -> bytes:
    src = array.array("h", pcm[: len(pcm) // 2 * 2])
    out = array.array("h", bytes(len(src) // 3 * 4))
    for i in range(len(out)):
        pos = i * 1.5
        j = int(pos)
        a = src[j]
        b = src[j + 1] if j + 1 < len(src) else a
        out[i] = int(a + (b - a) * (pos - j))
    return out.tobytes()


def question_audio(q: dict, key: str) -> bytes:
    """16 kHz PCM of the question, synthesized once and cached."""
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{q['id']}.pcm"
    if not path.exists():
        pcm = speech.gemini_pcm(q["text"], key, voice=QUESTION_VOICE, style="Say plainly:")
        path.write_bytes(resample_24k_to_16k(pcm))
    return trim(path.read_bytes())


def trim(pcm: bytes, threshold: int = 500) -> bytes:
    """Cut leading/trailing silence, so 'end of speech' is the last spoken sample."""
    samples = array.array("h", pcm[: len(pcm) // 2 * 2])
    loud = [i for i in range(0, len(samples), 160) if max(map(abs, samples[i:i + 160]), default=0) > threshold]
    if not loud:
        return pcm
    return samples[loud[0]:loud[-1] + 160].tobytes()


async def ask(q: dict, pcm: bytes, key: str, silence_ms: int | None, end_high=False, model="") -> dict:
    adapter = gemini_live.GeminiLiveAdapter(key, model=model, language=gemini_live.configured_language(),
                                            search=gemini_live.configured_search())
    setup = adapter._setup_message

    def patched(model, prompt):
        message = setup(model, prompt)
        vad = message["setup"]["realtimeInputConfig"]["automaticActivityDetection"]
        if silence_ms is not None:
            vad["silenceDurationMs"] = silence_ms
        if end_high:
            vad["endOfSpeechSensitivity"] = "END_SENSITIVITY_HIGH"
        return message
    adapter._setup_message = patched
    calls, said, heard = [], [], []
    first_audio, done = asyncio.Event(), asyncio.Event()
    t = {"audio": None}

    def stub(name):
        def handler(args):
            calls.append({"name": name, "args": args})
            return STUB.get(name, {"ok": True})
        return handler

    real = gemini_live.willie_tool_list(None, web_search=not adapter.search)
    tools = [Tool(x.name, x.description, x.parameters, handler=stub(x.name)) for x in real]
    tools.append(Tool(gemini_live.NOT_FOR_ME.name, gemini_live.NOT_FOR_ME.description,
                      gemini_live.NOT_FOR_ME.parameters, handler=stub(gemini_live.NOT_FOR_ME.name)))

    def on_audio(chunk):
        if t["audio"] is None:
            t["audio"] = time.perf_counter()
            first_audio.set()

    async def on_event(kind, detail=""):
        if kind == "said":
            said.append(detail)
        elif kind == "heard":
            heard.append(detail)
        elif kind in ("turn_complete", "closed", "error"):
            done.set()

    adapter.on_audio(on_audio)
    adapter.on_event(on_event)
    setup_started = time.perf_counter()
    await adapter.start_session(system_prompt(gemini_live.LIVE_EXTRA), gemini_live.live_context(), tools)
    setup_s = time.perf_counter() - setup_started
    step = 16_000 * 2 * CHUNK_MS // 1000
    for i in range(0, len(pcm), step):
        await adapter.send_audio(pcm[i:i + step])
        await asyncio.sleep(CHUNK_MS / 1000)
    speech_end = time.perf_counter()
    silence = bytes(step)
    while not first_audio.is_set() and time.perf_counter() - speech_end < 20:
        await adapter.send_audio(silence)
        try:
            await asyncio.wait_for(first_audio.wait(), CHUNK_MS / 1000)
        except asyncio.TimeoutError:
            pass
    try:
        await asyncio.wait_for(done.wait(), 30)
    except asyncio.TimeoutError:
        pass
    await asyncio.sleep(2)            # tool answers come in a second turn; collect its words too
    await adapter.close()
    answer = "".join(said).strip()
    names = [c["name"] for c in calls]
    want = q.get("tool", "none")
    tool_ok = (not names) if want == "none" else (True if want == "any" else want in names)
    expect = q.get("expect") or []
    words_ok = any(w in answer.lower() for w in expect) if expect else None
    return {
        "id": q["id"], "question": q["text"], "heard": "".join(heard).strip(),
        "latency_s": None if t["audio"] is None else round(t["audio"] - speech_end, 2),
        "setup_s": round(setup_s, 2), "model": adapter.model.split("/")[-1],
        "tools": calls, "tool_expected": want, "tool_ok": tool_ok, "words_ok": words_ok, "answer": answer,
    }


def report(results: list[dict], silence_ms) -> str:
    lat = sorted(r["latency_s"] for r in results if r["latency_s"] is not None)
    tool_pass = sum(r["tool_ok"] for r in results)
    words = [r["words_ok"] for r in results if r["words_ok"] is not None]
    lines = [
        f"# G1 run {datetime.now():%Y-%m-%d %H:%M} - {results[0]['model'] if results else '?'}"
        f"{f', silence {silence_ms} ms' if silence_ms is not None else ''}",
        "",
        f"- reply latency (end of question -> first sound): median **{statistics.median(lat):.2f} s**, "
        f"p90 {lat[int(len(lat) * .9) - 1 if len(lat) > 1 else 0]:.2f} s, min {lat[0]:.2f} s (n={len(lat)})" if lat else "- no audio answers",
        f"- tool calling: **{tool_pass}/{len(results)}** as expected",
        f"- fact checks: {sum(words)}/{len(words)}" if words else "- fact checks: none",
        "",
        "| id | latency | tools (expected) | ok | answer |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        called = ", ".join(c["name"] for c in r["tools"]) or "-"
        ok = ("✓" if r["tool_ok"] else "✗") + ("" if r["words_ok"] is None else (" ✓" if r["words_ok"] else " ✗fact"))
        lines.append(f"| {r['id']} | {r['latency_s']} | {called} ({r['tool_expected']}) | {ok} | "
                     f"{r['answer'][:140].replace('|', '/')} |")
    return "\n".join(lines)


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ids", nargs="*")
    parser.add_argument("--silence", type=int, help="silenceDurationMs for this run (default: the adapter's)")
    parser.add_argument("--end-high", action="store_true", help="endOfSpeechSensitivity HIGH instead of LOW")
    parser.add_argument("--model", default="", help="one Live model instead of the adapter's list")
    args = parser.parse_args()
    load_env()
    import os
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("GEMINI_API_KEY missing")
    questions = yaml.safe_load((Path(__file__).parent / "questions.yaml").read_text())
    if args.ids:
        questions = [q for q in questions if q["id"] in args.ids]
    results = []
    for q in questions:
        try:
            r = await ask(q, question_audio(q, key), key, args.silence, args.end_high, args.model)
        except Exception as exc:
            r = {"id": q["id"], "question": q["text"], "latency_s": None, "tools": [], "tool_expected": q.get("tool"),
                 "tool_ok": False, "words_ok": None, "answer": f"FOUT {type(exc).__name__}: {exc}", "model": "?"}
        results.append(r)
        print(f"{r['id']:<11} {r['latency_s']!s:>5} s  tools={[c['name'] for c in r['tools']]}  {r['answer'][:90]}", flush=True)
        await asyncio.sleep(1)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    (OUT / f"run-{stamp}.json").write_text(json.dumps(results, ensure_ascii=False, indent=1))
    text = report(results, args.silence)
    (OUT / f"run-{stamp}.md").write_text(text)
    print("\n" + text)


if __name__ == "__main__":
    asyncio.run(main())
