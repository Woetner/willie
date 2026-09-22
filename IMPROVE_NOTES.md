# Improve notes

## Request: set voice.wake_gain to 12.0 in config/willie.yaml (2026-09-22)

Not implemented — `config/willie.yaml` has no `voice.wake_gain` key, and it was
never 8.0. Repo-wide search for `wake_gain` found no matches anywhere
(config, schema, code).

The closest related setting is `voice.wake_sensitivity` (currently `0.5`),
defined in `config/schema.yaml` as a float with `min: 0.1, max: 0.95`. Setting
it to `12.0` would be out of schema range, and it's a threshold (lower =
more sensitive), not a gain, so mapping "increase gain to 12.0" onto it isn't
a safe guess.

To proceed, please confirm one of:
- The intended key is `voice.wake_sensitivity`, and if so what value (must be
  within 0.1–0.95, and note lower values increase sensitivity).
- A new `voice.wake_gain` setting should be added, and if so what it should
  do, its valid range, and whether anything downstream (wake-word engine)
  actually reads it — currently nothing does.
