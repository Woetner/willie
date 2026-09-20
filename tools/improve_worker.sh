#!/usr/bin/env bash
# Run on the MAC. Picks up code changes WILL-E was asked for and lets Claude
# Code make them - on a branch, in a worktree, never on the running robot.
#
#   bash tools/improve_worker.sh          # one pass over the queue
#   bash tools/improve_worker.sh --watch  # keep polling every 30 s
#
# Why it polls the Pi instead of the Pi pushing: the laptop needs no inbound
# access and the Pi needs no key to it. The robot only ever appends a line to a
# file (willie/voice/tools.py: verbeter_jezelf).
#
# What it does NOT do: deploy. Read the branch, then `make deploy` yourself.
set -euo pipefail

PI="${PI:-willie.local}"
PI_DIR="${PI_DIR:-willie}"
QUEUE=".local/improve_queue.jsonl"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORKTREES="$REPO/.local/worktrees"

command -v claude >/dev/null || { echo "claude CLI not found on this Mac"; exit 1; }

run_one() {
  local task="$1"
  local slug stamp branch tree
  stamp="$(date +%Y%m%d-%H%M%S)"
  slug="$(printf '%s' "$task" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | cut -c1-40 | sed 's/-$//')"
  branch="willie/auto-$stamp-$slug"
  tree="$WORKTREES/$stamp"

  echo "== $task"
  echo "   branch $branch"
  git -C "$REPO" fetch --quiet origin || true
  git -C "$REPO" worktree add --quiet -b "$branch" "$tree" origin/main 2>/dev/null \
    || git -C "$REPO" worktree add --quiet -b "$branch" "$tree" main

  # Claude Code works only inside the worktree. No deploy, no secrets: .env is
  # not in the repository and the Pi is not reachable from the prompt.
  ( cd "$tree" && claude -p "$(cat <<PROMPT
You are improving WILL-E, a Raspberry Pi robot. Read WILL-E.md and CLAUDE.md in
the parent project for context if they are available, and follow the repository
conventions.

Task, as spoken by Wouter to the robot:
$task

Rules:
- Change only what the task asks for. Keep it small and reviewable.
- Never touch .env, credentials, or anything under /boot.
- The Pi is not available from here: do not try to deploy, ssh, or test on hardware.
- If the task is too vague to implement safely, write your questions into
  IMPROVE_NOTES.md instead of guessing.
- Commit your work with a clear message.
PROMPT
)" ) || echo "   (claude exited non-zero - look at the worktree)"

  if [ -n "$(git -C "$tree" status --porcelain)" ]; then
    git -C "$tree" add -A
    git -C "$tree" commit -qm "willie: $task" || true
  fi
  if git -C "$tree" log --oneline origin/main..HEAD 2>/dev/null | grep -q .; then
    git -C "$tree" push -q -u origin "$branch" && echo "   pushed $branch"
    echo "   review:  git diff main..$branch"
  else
    echo "   nothing changed; removing the branch"
    git -C "$REPO" worktree remove --force "$tree"
    git -C "$REPO" branch -D "$branch" >/dev/null 2>&1 || true
  fi
}

pass() {
  local queued
  queued="$(ssh "$PI" "test -f $PI_DIR/$QUEUE && grep -c . $PI_DIR/$QUEUE || echo 0")"
  [ "$queued" -gt 0 ] || return 0
  # Take the whole queue and clear it in one step, so a second worker cannot
  # pick up the same task.
  local tasks
  tasks="$(ssh "$PI" "cat $PI_DIR/$QUEUE && : > $PI_DIR/$QUEUE")"
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    task="$(printf '%s' "$line" | python3 -c 'import json,sys; print(json.load(sys.stdin)["opdracht"])' 2>/dev/null)" || continue
    run_one "$task"
  done <<< "$tasks"
}

mkdir -p "$WORKTREES"
if [ "${1:-}" = "--watch" ]; then
  echo "watching $PI for requests - Ctrl-C to stop"
  while true; do pass; sleep 30; done
else
  pass
  echo "done"
fi
