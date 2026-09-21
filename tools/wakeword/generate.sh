#!/bin/bash
# Step 1: synthetic "hey willie" samples + look-alike negatives with Piper (en_US LibriTTS-R, ~900 speakers).
# Runs in the training workspace: willie/.local/wakeword (see README.md).
set -euo pipefail
W="$(cd "$(dirname "$0")/../.." && pwd)/.local/wakeword"
cd "$W"
PY=.venv/bin/python
GEN=piper-sample-generator/generate_samples.py
N=${N:-8000}          # positives; the notebook uses 1000, more speakers x speeds = more robust
NEG=${NEG:-300}       # per look-alike phrase

NL=${NL:-4000}        # Dutch-pronounced positives: Wouter says it with a Dutch accent
SHORT=${SHORT:-3000}  # "Willie" on its own also wakes him (Wouter, 21 Sep): per language

# Positives: English (~900 speakers) and Dutch (52 speakers) voices, three speaking speeds.
if [ ! -d samples/positive_en ] || [ "$(ls samples/positive_en | wc -l)" -lt "$N" ]; then
  $PY $GEN "hey willie" --max-samples "$N" --batch-size 100 \
    --length-scales 0.85 1.0 1.2 --noise-scales 0.667 0.9 --noise-scale-ws 0.8 1.0 \
    --output-dir samples/positive_en
fi
if [ ! -d samples/positive_nl ] || [ "$(ls samples/positive_nl | wc -l)" -lt "$NL" ]; then
  $PY $GEN "hé willie" --model piper-sample-generator/models/nl_NL-mls-medium.pt \
    --max-samples "$NL" --batch-size 50 \
    --length-scales 0.85 1.0 1.2 --noise-scales 0.667 0.9 --noise-scale-ws 0.8 1.0 \
    --output-dir samples/positive_nl
fi

# "Willie" alone, English and Dutch voices.
if [ ! -d samples/positive_short_en ] || [ "$(ls samples/positive_short_en | wc -l)" -lt "$SHORT" ]; then
  $PY $GEN "willie" --max-samples "$SHORT" --batch-size 100 \
    --length-scales 0.85 1.0 1.2 --noise-scales 0.667 0.9 --noise-scale-ws 0.8 1.0 \
    --output-dir samples/positive_short_en
fi
if [ ! -d samples/positive_short_nl ] || [ "$(ls samples/positive_short_nl | wc -l)" -lt "$SHORT" ]; then
  $PY $GEN "willie" --model piper-sample-generator/models/nl_NL-mls-medium.pt \
    --max-samples "$SHORT" --batch-size 50 \
    --length-scales 0.85 1.0 1.2 --noise-scales 0.667 0.9 --noise-scale-ws 0.8 1.0 \
    --output-dir samples/positive_short_nl
fi

# Look-alikes the model must NOT wake on (openWakeWord-style adversarial negatives).
for phrase in "hey billy" "hey will" "hey william" "hey milly" "hey lily" "hey silly" \
              "hey really" "hey willow" "hey jill" "hey wheelie" "hey siri" "hey jimmy" \
              "hey dilly" "a willing" "they will be" "hey whistle"; do
  dir="samples/negative/${phrase// /_}"
  [ -d "$dir" ] && [ "$(ls "$dir" | wc -l)" -ge "$NEG" ] && continue
  $PY $GEN "$phrase" --max-samples "$NEG" --batch-size 50 --length-scales 0.85 1.0 1.2 --output-dir "$dir"
done
# Dutch look-alikes: with "Willie" alone as a wake word, everyday Dutch gets close.
for phrase in "wil je" "willen" "Willem" "wie is dat" "villa" "Billie" "wel even" "wiebelen" \
              "hé wil" "ik wil het" "Lily" "gezellig"; do
  dir="samples/negative/nl_${phrase// /_}"
  [ -d "$dir" ] && [ "$(ls "$dir" | wc -l)" -ge "$NEG" ] && continue
  $PY $GEN "$phrase" --model piper-sample-generator/models/nl_NL-mls-medium.pt \
    --max-samples "$NEG" --batch-size 50 --length-scales 0.85 1.0 1.2 --output-dir "$dir"
done
echo "positives: $(ls samples/positive_en samples/positive_nl | grep -c wav)  negatives: $(find samples/negative -name '*.wav' | wc -l)"
