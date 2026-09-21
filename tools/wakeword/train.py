"""Step 3: train "hey willie" and export the streaming model for pymicro-wakeword.

Settings follow microWakeWord's basic_training_notebook.ipynb, plus the look-alike negative
set from generate.sh. Output: config/wakewords/hey_willie.tflite + hey_willie.json.

    .local/wakeword/.venv/bin/python tools/wakeword/train.py [steps]
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
W = REPO / ".local" / "wakeword"
OUT = REPO / "config" / "wakewords"


def negative(path: str, weight: float, truncation: str = "random") -> dict:
    return {"features_dir": path, "sampling_weight": weight, "penalty_weight": 1.0,
            "truth": False, "truncation_strategy": truncation, "type": "mmap"}


def main() -> int:
    os.chdir(W)
    steps = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    config = {
        "window_step_ms": 10,
        "train_dir": "trained_models/hey_willie",
        "features": [
            {"features_dir": "features/positive", "sampling_weight": 2.0, "penalty_weight": 1.0,
             "truth": True, "truncation_strategy": "truncate_start", "type": "mmap"},
            negative("features/lookalike", 3.0, "truncate_start"),
            negative("negative_datasets/speech", 10.0),
            negative("negative_datasets/dinner_party", 10.0),
            negative("negative_datasets/no_speech", 5.0),
            negative("negative_datasets/dinner_party_eval", 0.0, "split"),   # validation/testing only
        ],
        "training_steps": [steps],
        "positive_class_weight": [1],
        "negative_class_weight": [20],
        "learning_rates": [0.001],
        "batch_size": 128,
        "time_mask_max_size": [0], "time_mask_count": [0],
        "freq_mask_max_size": [0], "freq_mask_count": [0],
        "eval_step_interval": 500,
        "clip_duration_ms": 1500,
        "target_minimization": 0.9,
        "minimization_metric": None,
        "maximization_metric": "average_viable_recall",
    }
    Path("training_parameters.yaml").write_text(yaml.dump(config))
    subprocess.run([
        sys.executable, "-m", "microwakeword.model_train_eval",
        "--training_config=training_parameters.yaml", "--train", "1", "--restore_checkpoint", "1",
        "--test_tf_nonstreaming", "0", "--test_tflite_nonstreaming", "0",
        "--test_tflite_nonstreaming_quantized", "0", "--test_tflite_streaming", "0",
        "--test_tflite_streaming_quantized", "1", "--use_weights", "best_weights",
        "mixednet", "--pointwise_filters", "64,64,64,64", "--repeat_in_block", "1, 1, 1, 1",
        "--mixconv_kernel_sizes", "[5], [7,11], [9,15], [23]", "--residual_connection", "0,0,0,0",
        "--first_conv_filters", "32", "--first_conv_kernel_size", "5", "--stride", "3",
    ], check=True)

    model = W / "trained_models/hey_willie/tflite_stream_state_internal_quant/stream_state_internal_quant.tflite"
    OUT.mkdir(parents=True, exist_ok=True)
    shutil.copy(model, OUT / "hey_willie.tflite")
    # Same manifest shape as the stock pymicro-wakeword / ESPHome v2 models. The cutoff is a
    # starting point: tune it on real recordings (D2 tests), not on the synthetic test set.
    (OUT / "hey_willie.json").write_text(json.dumps({
        "type": "micro", "wake_word": "hey willie", "author": "WILL-E project",
        "website": "https://github.com/woetner/willie", "model": "hey_willie.tflite",
        "trained_languages": ["en", "nl"], "version": 2,
        "micro": {"probability_cutoff": 0.97, "sliding_window_size": 5, "feature_step_size": 10,
                  "tensor_arena_size": 30000, "minimum_esphome_version": "2024.7.0"},
    }, indent=2) + "\n")
    print(f"exported {OUT / 'hey_willie.tflite'} ({(OUT / 'hey_willie.tflite').stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
