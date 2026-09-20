# Wake word

WILL-E listens for **"hey gemini"** with [Vosk](https://alphacephei.com/vosk/):
offline, free, no account and no training. Porcupine was the first choice for
its size, but its free tier is company-only now.

The model is **English**, although WILL-E speaks Dutch: "gemini" is an English
name and Wouter pronounces it the English way. Measured on a recording of him
saying it three times, the English model returns "gemini" 3/3 and the Dutch
model hears nothing - Dutch has no such word.

The model is not in git - it is 68 MB of third-party data. Install it on the Pi:

```bash
mkdir -p ~/willie/.local/models && cd ~/willie/.local/models
curl -sSLO https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
unzip -q vosk-model-small-en-us-0.15.zip && rm vosk-model-small-en-us-0.15.zip
```

`WILLIE_VOSK_MODEL` overrides the path if you want a different model - the
larger Dutch models are more accurate and much heavier.

Accuracy comes from the **grammar**, not the model size: Vosk is given a closed
vocabulary of the wake phrases plus `[unk]`, so it only decides whether what it
heard was one of them instead of transcribing everything. Spelling variants of
"gemini" are listed in `willie/voice/wake.py`; add more if it mishears yours.
