# Request: speel_video (YouTube search + play on Visual Workspace)

No code changed. This needs decisions that are not mine to make, and the
feature does not fit safely into `willie/` alone as currently built.

## What I found

1. **The robot's own screen has no video/web capability.** Per D3/D20 (WILL-E.md),
   the 3.5" face screen is written directly to the framebuffer (raw pixels), with
   no pygame, no OpenCV, no browser. It shows eyes, mood, short text and still
   photos (`toon`, `toon_afbeelding` in `willie/voice/gemini_live.py`). There is
   no way to play a YouTube video on it.

2. **"Visual Workspace" lives outside this repo.** The closest matching surfaces
   (Command UI's "visualisations" workspace, `/scherm`, the Canvas pages from
   Phase X / D40) are built and served by the **separate `homeserver` repo**
   (`hub/`, not `willie/`). This worktree only has `willie/`, so I cannot touch
   that code even if the feature were otherwise ready.

3. **Canvas pages deliberately forbid exactly what embedding needs.** X2 (Phase X,
   WILL-E.md) states Canvas pages must have "no scripts, no requests of any
   kind... a self-redirect in the page is defused" — a security boundary so a
   page can't act as Wouter. A YouTube `<iframe>` embed needs outside requests
   and script, which runs straight into that boundary. Lifting it for this one
   feature is a security decision, not a code change I should make unilaterally.

4. **No existing `video` means YouTube anywhere in the repo.** The only `video`
   concept in `willie/` is the live camera stream (`willie/remote.py`,
   `willie/cmd/video`) — unrelated to media playback.

5. **YouTube search needs a provider decision.** Either the YouTube Data API
   (needs its own API key + quota/cost, like the Gemini key convention of
   living only in `.env`) or scraping (fragile, against YouTube's terms). Which
   one, and whether it is worth a new paid/metered dependency, is Wouter's call
   (the project tracks exactly this kind of choice as a gate, e.g. D9/G1).

## What I'd need to know before building this
- Which surface is "the screen" here: the robot's own 480×320 face, the
  homeserver's `/scherm` / Command visualisations workspace, or the garage
  monitor? They have very different capabilities (none of them currently
  play video).
- If it's a homeserver surface: should Canvas's "no outside requests" rule be
  relaxed just for a YouTube embed (and is a sandboxed `<iframe>` with a fixed
  `youtube-nocookie.com` origin an acceptable exception), or should this be a
  dedicated new page type instead of a Canvas page?
- Which YouTube search method is OK: official Data API (new key + quota/cost)
  or something else?
- Is this meant to ship now (robot has no screen for it) or is it really a
  homeserver/Command-UI feature that happens to be triggered by voice? If the
  latter, it belongs in the `homeserver` repo, not `willie/`.

Until these are answered, nothing changed here.
