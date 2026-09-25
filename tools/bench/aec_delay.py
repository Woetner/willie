#!/usr/bin/env python3
"""Echo canceller bench (25 Sep): speaker-to-mic delay, echo reduction, CPU.

Run on the Pi with the voice service stopped:  make bench-aec

Plays, through the live session's own Speaker class (same aplay, same timing model):
two noise bursts (the first opens aplay, the second comes after a gap, as a new answer
does) and a stretch of speech. The mic records through the same path as the live
session. Then, offline:
- the delay between what the reference timeline says was played and what the mic heard,
  per burst -> voice.aec_delay_ms (if the two bursts differ a lot, the delay is not fixed)
- the echo peak going into speex (a clipped echo cannot be cancelled)
- echo reduction on the speech (ERLE) and the loudest leftover per 100 ms chunk after
  cleaning -> voice.barge_in must sit above that
- CPU per second of audio for decimation and cleaning
"""
from __future__ import annotations

import asyncio
import sys
import time
import wave
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from willie.audio import clean, mic  # noqa: E402
from willie.voice.gemini_live import OUT_RATE, Speaker  # noqa: E402

RATE = clean.RATE
SPEECH = REPO / ".local" / "wakeword_rec" / "negative" / "talk_135420.wav"


def speech_24k(seconds: float) -> np.ndarray:
    if SPEECH.exists():
        with wave.open(str(SPEECH)) as w:
            data = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32)
            rate = w.getframerate()
    else:                                        # no recording: a vowel-ish buzz
        rate = RATE
        t = np.arange(int(seconds * rate)) / rate
        data = np.sign(np.sin(2 * np.pi * 140 * t)) * 3000 * (np.sin(2 * np.pi * 3 * t) > 0)
    data = data[: int(seconds * rate)]
    data = data / max(1.0, np.abs(data).max()) * 20_000
    return np.interp(np.arange(int(len(data) * OUT_RATE / rate)) * (rate / OUT_RATE),
                     np.arange(len(data)), data).astype(np.int16)


async def record_while_playing(plan: list[tuple[float, np.ndarray]], seconds: float):
    loop = asyncio.get_running_loop()
    reference = clean.EchoReference(delay=0.0)
    speaker = Speaker(echo=reference)
    process = await asyncio.create_subprocess_exec(*mic.command(), stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.DEVNULL)
    stream = mic.Stream(1.0)
    await process.stdout.readexactly(int(RATE * 0.8) * mic.FRAME)          # settling
    chunks, captured, t0 = [], 0, float("inf")
    decimate_s = 0.0

    async def play() -> None:
        started = loop.time()
        for at, pcm in plan:
            await asyncio.sleep(max(0.0, started + at - loop.time()))
            step = OUT_RATE // 25                                          # 40 ms pieces,
            for i in range(0, len(pcm), step):                              # 2x real time,
                speaker.write(pcm[i:i + step].tobytes())                    # like the network
                await asyncio.sleep(0.02)

    player = asyncio.create_task(play())
    while captured < seconds * RATE:
        raw = await process.stdout.readexactly(1600 * mic.FRAME)
        begin = time.process_time()
        decimated = stream.decimate(raw)
        decimate_s += time.process_time() - begin
        captured += len(decimated)
        t0 = min(t0, loop.time() - captured / RATE)
        chunks.append(decimated)
    await player
    process.terminate()
    await process.wait()
    speaker.stop()
    heard = np.concatenate(chunks)
    played = reference.read(t0, len(heard)).astype(np.float32)
    return heard, played, decimate_s


def lag_ms(heard: np.ndarray, played: np.ndarray, start: float, end: float) -> tuple[float, float]:
    """Delay of `heard` behind `played` in [start, end) s, and the correlation peak's strength."""
    window = slice(int(start * RATE), int(end * RATE))
    seconds, clarity = clean.lag(heard[window], played[window])
    return seconds * 1000, clarity


def shift(signal: np.ndarray, samples: int) -> np.ndarray:
    out = np.zeros_like(signal)
    if samples >= 0:
        out[samples:] = signal[: len(signal) - samples]
    else:
        out[:samples] = signal[-samples:]
    return out


def main() -> int:
    if clean.library() is None:
        print("libspeexdsp missing: sudo apt install libspeexdsp1", file=sys.stderr)
        return 2
    rng = np.random.default_rng(7)
    noise = (rng.standard_normal(int(1.5 * OUT_RATE)) * 6000).astype(np.int16)
    talk = speech_24k(9.0)
    plan = [(0.5, noise), (3.5, noise), (6.5, talk)]
    print("playing noise, noise, speech (~16 s) - keep the room quiet")
    heard, played, decimate_s = asyncio.run(record_while_playing(plan, 17.0))

    first, strength1 = lag_ms(heard, played, 0.3, 2.5)
    second, strength2 = lag_ms(heard, played, 3.3, 5.5)
    print(f"delay burst 1 (fresh aplay): {first:6.1f} ms  (peak {strength1:.0f}x median)")
    print(f"delay burst 2 (after a gap): {second:6.1f} ms  (peak {strength2:.0f}x median)")
    delay = second
    at_in = heard * clean.IN_GAIN
    print(f"echo peak into speex (x{clean.IN_GAIN:g}): {np.abs(at_in).max() / 327.68:.1f} % FS"
          f"   at the wake gain x{mic.gain():g}: {np.abs(heard * mic.gain()).max() / 327.68:.0f} % FS")

    speech = slice(int(6.8 * RATE), int(15.0 * RATE))
    mic_in = np.clip(at_in, -32768, 32767).astype(np.int16)
    reference = shift(played, int(round(delay * RATE / 1000))).astype(np.int16)
    results = {}
    for name, echo in (("clean only", False), ("clean + AEC", True)):
        cleaner = clean.Cleaner.load(-15, echo=echo)
        begin = time.process_time()
        out = np.frombuffer(cleaner.process(mic_in.tobytes(), reference if echo else None), "<i2")
        cpu = time.process_time() - begin
        cleaner.close()
        results[name] = out
        seg = out[speech].astype(np.float32)
        peaks = np.abs(seg[: len(seg) // 1600 * 1600].reshape(-1, 1600)).max(axis=1) / 32768
        print(f"{name:12s}: leftover echo peak per 100 ms  median {np.median(peaks):.2f}  "
              f"max {peaks.max():.2f} FS   CPU {cpu / (len(out) / RATE) * 100:.1f} % of a core")
    erle = 10 * np.log10(np.mean(mic_in[speech].astype(np.float64) ** 2)
                         / (np.mean(results["clean + AEC"][speech].astype(np.float64) ** 2) + 1))
    print(f"echo reduction on speech (input vs output, incl. AGC): {erle:.1f} dB")
    print(f"decimation CPU: {decimate_s / (len(heard) / RATE) * 100:.1f} % of a core")
    print(f"-> voice.aec_delay_ms: {delay:.0f}")
    out_dir = REPO / ".local" / "aec"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, data in (("mic", mic_in), ("ref", reference), ("clean", results["clean only"]),
                       ("aec", results["clean + AEC"])):
        with wave.open(str(out_dir / f"{name}.wav"), "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(RATE)
            w.writeframes(np.asarray(data, np.int16).tobytes())
    print(f"wavs in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
