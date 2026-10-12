# Visual Workspace video playback — not implemented here

Request: let the Visual Workspace (`garage_werkruimte` visuals, the "visualisations"
panel in Command on the garage monitor) play videos (YouTube or a local file) next to
cards/images, for instruction videos during jobs like the Kever schakelstangbus swap.

## Why nothing changed in this repo

The Visual Workspace lives entirely in the `homeserver` repo, not in `willie`:
- UI: `homeserver/hub/static/garage.html`, `garage.css`, `garage-ui.js`, `garage-embed.css`
- Data/tool: `homeserver/hub/garage_ui.py`
- The `garage_werkruimte` tool dispatcher itself: `homeserver/hub/*` (see WILL-E.md
  Phase X5, "the hub's `garage_werkruimte` tool routes UI navigation through the
  existing tool dispatcher")

This worktree only contains the `willie` repo (the Pi robot: `willie/`, `firmware/`,
`tools/`, `tests/`, etc. — see `README.md`). There is no `homeserver` checkout
available here, and `willie/` has no code for Command, the garage monitor, or any
visualisations panel. Adding a video player here would mean guessing at files in a
repo I can't see and that this worktree doesn't track, so nothing was changed.

## What's needed to do this properly

- A worktree/session with the `homeserver` repo checked out (sibling to `willie/`,
  per WILL-E.md D16/D24/X5), so the actual `garage_ui.py` + `garage.html`/`garage-ui.js`
  can be read and edited.
- A decision from Wouter on scope: just a local video file bound into a step/card
  (similar to how X2's canvas pages or job-mode photos are stored), an embedded
  YouTube iframe, or both. X5's canvas policy currently forbids scripts/iframes/
  outside requests on shown pages (self-contained HTML + SVG only) for security
  reasons tied to the hub's trust-by-header model — embedding YouTube would need an
  explicit, deliberate exception to that policy.
- Confirmation of whether this is scoped under Phase X (X5 Garage monitor) or a new
  item, since X5's physical-acceptance gates are still open and the plan says not to
  tick X5 from software alone.
