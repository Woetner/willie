# Request not implemented: garage camera mic as voice input

Request: use the garage wifi camera's microphone as an extra/alternative audio
input for voice control (spraakbesturing), compared against the laptop mic,
accounting for wifi delay, reverb and distance, with the current mic as
fallback.

I changed nothing in code. This touches the voice pipeline's core path and
two fixed decisions, and needs answers only Wouter can give before any line
of code is safe to write.

## Why I stopped here

1. **D9 / D39 conflict.** D9: "Voice stays Pi → Gemini directly (latency)".
   D39 (Phase T, 8 Oct): "the voice must be reliable and clear, **no hardware
   changes**... every extra (local command lane, speaker check, second look
   at a wake, both mics) comes back only with a **before/after score on
   recorded clips**." Routing audio from a wall camera over wifi through the
   home server adds exactly the kind of extra hop D39 was written to avoid.
   §0.2.1 says to propose a change to a fixed decision and wait for Wouter's
   OK, not make it quietly.

2. **This was already tried and dropped.** TR6 ("Both mics", dropped 8 Oct):
   adding WILL-E's own second built-in mic did not help at the distance it
   was needed (4 m with the radio on: 49 % word error either way) because
   "the room's echoes are as strong as the direct sound there." The garage
   camera mic is farther from Wouter than the robot's own mics usually are,
   wall-mounted, and now adds wifi jitter/compression on top of the same
   room-echo problem. Nothing suggests it would do better; it could only be
   known from recorded-clip tests like the ones TR0–TR7 already ran.

3. **No audio path exists today to extend.** `willie/audio/mic.py` captures
   raw PCM straight from the I2S device via `arecord` and feeds it directly
   into the wake/Gemini Live pipeline — there is no pluggable "audio source"
   layer. The garage camera (Tapo C200, K1) is currently only read for
   **video** frames (RTSP/ONVIF, server-side `hub/garage.py`, not in this
   repo); nobody has confirmed it exposes a usable continuous **audio**
   stream, what codec, or what latency it adds over wifi. Finding that out
   needs the real camera and the real network, which I cannot reach from
   here (no SSH, no hardware).

4. **Risk tag.** The robot itself flagged this request "hoog" (high) risk,
   which matches points 1–3: it would change the live voice path real-time
   speech depends on.

## What I'd need before implementing anything

- Wouter's OK to treat this as a change to D9 (voice path) and D39 (no
  hardware changes to hearing without a before/after score), logged in
  WILL-E.md §13 if approved.
- Confirmation the Tapo C200 (or whichever garage camera) actually exposes an
  audio stream, and over what protocol (RTSP audio track? ONVIF?).
- A measurement step first, same method as TR0: recorded clips from the
  garage camera mic vs. the laptop mic vs. the existing INMP441 mics, scored
  on word error and end-to-end latency, *before* any adapter code is written
  — not implementation first.
- A decision on fallback order and how a mode switch (garage mode, D25) would
  pick the input without adding a layer to the "one straight path" that
  D39 just finished simplifying.

Once those are answered this becomes a normal, scoped step (probably under
Phase K or a new Phase T step) rather than a blind architecture change.
