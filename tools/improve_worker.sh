#!/usr/bin/env bash
# Runs on the MAC, unattended. Wouter never touches it: he approves by voice on
# the robot, and this picks the job up and carries it out end to end.
#
#   bash tools/improve_worker.sh --watch     # normal use (or: make improve-watch)
#   bash tools/improve_worker.sh             # one pass
#
# Per request: branch in a worktree -> Claude Code implements -> tests ->
# deploy to the Pi -> health check -> roll back if the robot does not come back
# up. The outcome is written back to the Pi so WILL-E can say how it went.
#
# It polls the Pi, so the laptop needs no inbound access and the Pi holds no key
# to it. The only thing crossing from the robot is a line of text.
set -uo pipefail

PI="${PI:-willie.local}"
PI_DIR="${PI_DIR:-willie}"
QUEUE=".local/improve_queue.jsonl"
RESULTS=".local/improve_results.jsonl"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORKTREES="$REPO/.local/worktrees"

command -v claude >/dev/null || { echo "claude CLI not found on this Mac"; exit 1; }

report() {  # report <status> <text>
  local line
  line="$(python3 "$REPO/tools/improve_report.py" "$1" "$2")"
  ssh "$PI" "printf '%s\n' $(printf '%q' "$line") >> $PI_DIR/$RESULTS" 2>/dev/null || true
  echo "   -> $1: $2"
}

health_ok() {
  # The robot is healthy when its service is running and the voice stack imports.
  ssh "$PI" "systemctl is-active --quiet willie && cd $PI_DIR && .venv/bin/python -c 'import willie.voice.gemini_live, willie.voice.tools, willie.audio.speech' " >/dev/null 2>&1
}

deploy_tree() {  # deploy_tree <path>
  rsync -az --delete \
    --exclude .git/ --exclude .env --exclude .venv/ --exclude __pycache__/ \
    --exclude '*.log' --exclude .DS_Store --exclude firmware/.pio/ --exclude .local/ \
    "$1/" "$PI:$PI_DIR/" >/dev/null 2>&1 || return 1
  ssh "$PI" "sudo systemctl restart willie" >/dev/null 2>&1
  sleep 4
}

run_one() {
  local task="$1" plan="$2" risk="$3"
  local stamp slug branch tree
  stamp="$(date +%Y%m%d-%H%M%S)"
  slug="$(printf '%s' "$task" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | cut -c1-40 | sed 's/-$//')"
  branch="willie/auto-$stamp-$slug"
  tree="$WORKTREES/$stamp"

  echo "== [$risk] $task"
  git -C "$REPO" fetch --quiet origin 2>/dev/null
  git -C "$REPO" worktree add --quiet -b "$branch" "$tree" main || { report mislukt "kon geen branch maken"; return; }

  local prompt
  prompt="$(cat "$REPO/tools/improve_prompt.txt")"
  prompt="${prompt//__TASK__/$task}"
  prompt="${prompt//__PLAN__/$plan}"
  prompt="${prompt//__RISK__/$risk}"
  ( cd "$tree" && claude -p "$prompt" ) >/dev/null 2>&1

  if [ -n "$(git -C "$tree" status --porcelain)" ]; then
    git -C "$tree" add -A && git -C "$tree" commit -qm "willie: $task"
  fi
  if ! git -C "$tree" log --oneline main..HEAD 2>/dev/null | grep -q .; then
    report niets "er viel niets te veranderen, of de opdracht was te vaag"
    git -C "$REPO" worktree remove --force "$tree" 2>/dev/null
    git -C "$REPO" branch -D "$branch" >/dev/null 2>&1
    return
  fi
  if [ -f "$tree/IMPROVE_NOTES.md" ]; then
    report vraag "$(head -c 200 "$tree/IMPROVE_NOTES.md" | tr '\n' ' ')"
  fi

  # Does it at least parse? Cheapest possible test before it touches the robot.
  if ! ( cd "$tree" && python3 -m compileall -q willie tools >/dev/null 2>&1 ); then
    report mislukt "de code compileert niet, niets geinstalleerd"
    return
  fi

  # Keep what is running now, so we can put it back.
  local backup="$WORKTREES/rollback-$stamp"
  rm -rf "$backup" && mkdir -p "$backup"
  rsync -az --exclude .venv/ --exclude .local/ "$PI:$PI_DIR/" "$backup/" >/dev/null 2>&1

  if deploy_tree "$tree" && health_ok; then
    git -C "$tree" push -q -u origin "$branch" 2>/dev/null
    report klaar "$plan"
  else
    echo "   health check failed - rolling back"
    deploy_tree "$backup"
    if health_ok; then
      report teruggedraaid "het werkte niet, ik heb de oude versie teruggezet"
    else
      report kapot "de installatie is mislukt en terugdraaien ook - kijk even op de Pi"
    fi
    git -C "$tree" push -q -u origin "$branch" 2>/dev/null
  fi
  rm -rf "$backup"
}

pass() {
  local tasks
  tasks="$(ssh "$PI" "test -s $PI_DIR/$QUEUE && cat $PI_DIR/$QUEUE && : > $PI_DIR/$QUEUE" 2>/dev/null)"
  [ -n "$tasks" ] || return 0
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    local parsed task plan risk
    parsed="$(printf '%s' "$line" | python3 "$REPO/tools/improve_parse.py" 2>/dev/null)" || continue
    task="$(sed -n 1p <<< "$parsed")"
    plan="$(sed -n 2p <<< "$parsed")"
    risk="$(sed -n 3p <<< "$parsed")"
    [ -n "$task" ] && run_one "$task" "$plan" "$risk"
  done <<< "$tasks"
}

mkdir -p "$WORKTREES"
if [ "${1:-}" = "--watch" ]; then
  echo "watching $PI - Ctrl-C to stop"
  while true; do pass; sleep 20; done
else
  pass; echo "done"
fi
