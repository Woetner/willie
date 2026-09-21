# Training "Hey Willie" (D2)

microWakeWord, trained on the Mac (M1 Pro, CPU/MPS, no Colab). The result is two small
files in `config/wakewords/` that `willie/voice/wake.py` loads on the Pi.

Workspace (not in git, ~15 GB): `.local/wakeword/`.

## One-time setup

```bash
cd .local/wakeword                      # create it first: mkdir -p .local/wakeword
python3 -m venv .uvenv && .uvenv/bin/pip install uv
.uvenv/bin/uv python install 3.11 && .uvenv/bin/uv venv -p 3.11 .venv
git clone https://github.com/kahrendt/microWakeWord
git clone -b mps-support https://github.com/kahrendt/piper-sample-generator
# PyTorch >= 2.6 loads weights-only by default; the Piper .pt files need the old behaviour:
sed -i "" "s/torch.load(model_path)/torch.load(model_path, weights_only=False)/" piper-sample-generator/generate_samples.py
VIRTUAL_ENV=$PWD/.venv .uvenv/bin/uv pip install -e ./microWakeWord torch torchaudio piper-phonemize-cross==1.2.1 scipy
dl/fetch.sh                             # ~7 GB: negative feature sets, FMA, AudioSet part, Piper voice
```

Voices for the samples: `en_US-libritts_r-medium.pt` (~900 English speakers) and
`nl_NL-mls-medium.pt` (52 Dutch speakers), both from the piper-sample-generator v2.0.0 release,
into `piper-sample-generator/models/`.

## Train

```bash
tools/wakeword/generate.sh                                   # 1. Piper samples (positives + look-alikes)
.local/wakeword/.venv/bin/python tools/wakeword/features.py  # 2. augmentation + spectrograms
.local/wakeword/.venv/bin/python tools/wakeword/train.py     # 3. train, export to config/wakewords/
```

Then `make deps deploy` and test on the Pi with `.venv/bin/python tools/bench/wake.py 600`.

**Licence:** the negative datasets have mixed licences; like every microWakeWord model trained
this way, `hey_willie.tflite` is for non-commercial personal use.
