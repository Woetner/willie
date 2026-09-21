# Wake word

WILL-E listens for **"Hey Willie"** with [microWakeWord](https://github.com/kahrendt/microWakeWord)
through `pymicro-wakeword` (D2, D19): 32 MB RSS and ~5 % of one Pi 3 A+ core while listening.

- `hey_willie.tflite` — the trained streaming model (a few tens of kB)
- `hey_willie.json` — its manifest; `micro.probability_cutoff` is the sensitivity
  (higher = fewer false wakes, more misses). Tune it with `tools/bench/wake.py`.

How it was trained, and how to retrain: `tools/wakeword/README.md`.
Until the model exists, `willie/voice/wake.py` falls back to the stock "hey jarvis" model.

The earlier bench detector (Vosk, "hey gemini", 150 MB) is gone: it did not fit the RAM
budget (§5.5).
