#!/bin/sh
# librespot --onevent hook (willie-spotify.service): keeps WILL-E's player and sound-card
# state (and the track title) in tiny files, so the voice process knows without a network call whether
# music is playing and whether the card is free for his voice (willie/skills/spotify.py).
dir="$(dirname "$0")/../.local"
case "$PLAYER_EVENT" in
  sink) echo "$SINK_STATUS" > "$dir/spotify.sink" ;;
  playing|paused|stopped|unavailable) echo "$PLAYER_EVENT" > "$dir/spotify.player" ;;
  session_disconnected) echo stopped > "$dir/spotify.player" ;;
  # Now playing on the face: title, then the artists (one per line; a podcast has its show name).
  track_changed) printf '%s\n%s\n' "$NAME" "$(printf '%s' "${ARTISTS:-$SHOW_NAME}" | head -n 2 | paste -sd, - | sed 's/,/, /g')" > "$dir/spotify.track" ;;
esac
exit 0
