"""Step 2: augmentation data + spectrogram features for microWakeWord training.

Follows microWakeWord's basic_training_notebook.ipynb, with two additions for WILL-E:
Dutch-pronounced positives next to the English ones, and the look-alike phrases from
generate.sh as an extra negative set. Run with the training venv, from anywhere:

    .local/wakeword/.venv/bin/python tools/wakeword/features.py
"""
from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path

import numpy as np
import scipy.io.wavfile
from tqdm import tqdm

W = Path(__file__).resolve().parents[2] / ".local" / "wakeword"
os.chdir(W)


def wav16(path: str, array) -> None:
    scipy.io.wavfile.write(path, 16000, (np.clip(array, -1, 1) * 32767).astype(np.int16))


def augmentation_data() -> None:
    import datasets

    if not Path("mit_rirs").exists():
        Path("mit_rirs").mkdir()
        rirs = datasets.load_dataset("davidscripka/MIT_environmental_impulse_responses",
                                     split="train", streaming=True)
        for row in tqdm(rirs, desc="MIT RIRs"):
            wav16(f"mit_rirs/{row['audio']['path'].split('/')[-1]}", row["audio"]["array"])

    if not Path("audioset_16k").exists():
        # One of the AudioSet balanced-train parquet shards (the notebook's .tar is gone
        # from Hugging Face since the dataset moved to parquet): 500 clips of 10 s.
        # Read with pyarrow + soundfile: `datasets` 3.x cannot parse the shard's metadata
        # and 4.x needs torchcodec/FFmpeg.
        import pyarrow.parquet as pq
        Path("audioset_16k").mkdir()
        table = pq.read_table(W / "dl/audioset_bal09.parquet", columns=["video_id", "audio"]).to_pylist()
        for row in tqdm(table, desc="AudioSet"):
            wav16(f"audioset_16k/{row['video_id']}.wav", load16(io.BytesIO(row["audio"]["bytes"])))

    if not Path("fma_16k").exists():
        Path("fma").mkdir(exist_ok=True)
        subprocess.run(["unzip", "-q", "-o", str(W / "dl/fma_xs.zip"), "-d", "fma"], check=True)
        Path("fma_16k").mkdir()
        for path in tqdm(sorted(Path("fma").glob("**/*.mp3")), desc="FMA"):
            try:
                wav16(f"fma_16k/{path.stem}.wav", load16(str(path)))
            except Exception as exc:        # a few FMA files are truncated
                print(f"skip {path.name}: {exc}")


def load16(source):
    """Any soundfile-readable audio -> mono float32 at 16 kHz."""
    import librosa
    import soundfile
    audio, rate = soundfile.read(source, dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    return librosa.resample(audio, orig_sr=rate, target_sr=16000) if rate != 16000 else audio


def split_long(src: str, dst: str, seconds: float = 3.0) -> None:
    """Cut long recordings (Wouter talking, TV) into clips the size of a training window."""
    Path(dst).mkdir(parents=True, exist_ok=True)
    for path in sorted(Path(src).glob("*.wav")):
        rate, audio = scipy.io.wavfile.read(path)
        step = int(rate * seconds)
        for i in range(0, len(audio) - step + 1, step):
            scipy.io.wavfile.write(f"{dst}/{path.stem}_{i // step:03d}.wav", rate, audio[i:i + step])


def features(pattern: str, out: str, train_repeat: int = 2) -> None:
    from mmap_ninja.ragged import RaggedMmap
    from microwakeword.audio.augmentation import Augmentation
    from microwakeword.audio.clips import Clips
    from microwakeword.audio.spectrograms import SpectrogramGeneration

    if Path(out).exists():
        print(f"{out} exists, skipped")
        return
    clips = Clips(input_directory="samples", file_pattern=pattern, max_clip_duration_s=None,
                  remove_silence=False, random_split_seed=10, split_count=0.1)
    augmenter = Augmentation(
        augmentation_duration_s=3.2,
        augmentation_probabilities={
            "SevenBandParametricEQ": 0.1, "TanhDistortion": 0.1, "PitchShift": 0.1,
            "BandStopFilter": 0.1, "AddColorNoise": 0.1, "AddBackgroundNoise": 0.75,
            "Gain": 1.0, "RIR": 0.5,
        },
        impulse_paths=["mit_rirs"],
        background_paths=["fma_16k", "audioset_16k"],
        background_min_snr_db=-5, background_max_snr_db=10,
        min_jitter_s=0.195, max_jitter_s=0.205,
    )
    for split, name, repeat, slide in (("training", "train", train_repeat, 10),
                                       ("validation", "validation", 1, 10),
                                       ("testing", "test", 1, 1)):
        target = Path(out) / split
        target.mkdir(parents=True, exist_ok=True)
        spectrograms = SpectrogramGeneration(clips=clips, augmenter=augmenter, slide_frames=slide, step_ms=10)
        RaggedMmap.from_generator(out_dir=str(target / "wakeword_mmap"),
                                  sample_generator=spectrograms.spectrogram_generator(split=name, repeat=repeat),
                                  batch_size=100, verbose=True)


if __name__ == "__main__":
    augmentation_data()
    features("positive_*/*.wav", "features/positive")
    features("negative/*/*.wav", "features/lookalike")
    # Round 2: Wouter's own voice through the robot's mic (make wake-record / wake-fetch).
    # Few clips, so each is augmented many more times than the synthetic ones.
    if Path("samples/wouter").exists():
        features("wouter/*.wav", "features/wouter", train_repeat=40)
    if Path("samples/wouter_neg_long").exists():
        split_long("samples/wouter_neg_long", "samples/wouter_neg")
        features("wouter_neg/*.wav", "features/wouter_neg", train_repeat=10)
