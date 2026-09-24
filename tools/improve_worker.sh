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
QUESTIONS=".local/claude_questions.jsonl"   # vraag_claude
ANSWERS=".local/claude_answers.jsonl"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PLAN_FILE="$(dirname "$REPO")/WILL-E.md"   # master plan, one level above the repo
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

deploy_tree() {  # deploy_tree <path> [units]
  # "units" reinstalls the systemd unit files too. `make deploy` never does that,
  # so without it a change under systemd/ lands on disk, reports success and
  # changes nothing - what actually runs is the installed copy in
  # /etc/systemd/system.
  # The master plan sits outside the repo; send it along unless the tree (a backup
  # taken from the Pi) already holds a copy. Without it --delete removes it there.
  local plan=()
  [ -f "$1/WILL-E.md" ] || plan=("$PLAN_FILE")
  rsync -az --delete \
    --exclude .git/ --exclude .env --exclude .venv/ --exclude __pycache__/ \
    --exclude '*.log' --exclude .DS_Store --exclude firmware/.pio/ --exclude .local/ \
    "$1/" "${plan[@]}" "$PI:$PI_DIR/" >/dev/null 2>&1 || return 1
  if [ "${2:-}" = "units" ]; then
    ssh "$PI" "sudo bash $PI_DIR/tools/install_service.sh" >/dev/null 2>&1 \
      || echo "   (install_service.sh failed)"
  fi
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
  prompt="${prompt//__PLAN_FILE__/$PLAN_FILE}"
  # Keep what the agent said: without it a run that changes nothing is a mystery.
  local agentlog="$REPO/.local/logs/$stamp.log"
  mkdir -p "$REPO/.local/logs"
  # acceptEdits, because nobody is here to answer a permission prompt: a headless
  # run that asks for write access just stops and reports "nothing changed".
  # It is confined to a throwaway worktree, and its work still has to survive the
  # smoke test, the health check and the rollback before it reaches the robot.
  # The worktree is not next to WILL-E.md, so give the agent the plan's folder explicitly.
  ( cd "$tree" && claude -p "$prompt" \
      --add-dir "$(dirname "$PLAN_FILE")" \
      --permission-mode acceptEdits \
      --allowedTools "Edit" "Write" "Read" "Grep" "Glob" "Bash(git *)" "Bash(python3 *)" \
  ) < /dev/null > "$agentlog" 2>&1
  echo "   agent log: $agentlog"

  if [ -n "$(git -C "$tree" status --porcelain)" ]; then
    git -C "$tree" add -A && git -C "$tree" commit -qm "willie: $task"
  fi
  if ! git -C "$tree" log --oneline main..HEAD 2>/dev/null | grep -q .; then
    report niets "er viel niets te veranderen, of de opdracht was te vaag"
    echo "   agent said: $(tail -c 400 "$agentlog" 2>/dev/null | tr '\n' ' ')"
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

  # A change under systemd/ is inert until the units are reinstalled.
  local units=""
  if git -C "$tree" diff --name-only main..HEAD | grep -q "^systemd/"; then
    units="units"
    echo "   touches systemd - units will be reinstalled"
  fi

  if deploy_tree "$tree" "$units" && health_ok; then
    git -C "$tree" push -q -u origin "$branch" 2>/dev/null
    if [ -n "$units" ]; then
      report klaar "$plan (systemd opnieuw geinstalleerd)"
    else
      report klaar "$plan"
    fi
  else
    echo "   health check failed - rolling back"
    deploy_tree "$backup" "$units"
    if health_ok; then
      report teruggedraaid "het werkte niet, ik heb de oude versie teruggezet"
    else
      report kapot "de installatie is mislukt en terugdraaien ook - kijk even op de Pi"
    fi
    git -C "$tree" push -q -u origin "$branch" 2>/dev/null
  fi
  rm -rf "$backup"
}

ask_one() {  # ask_one <question>  - read-only Claude Code, answer goes back to the Pi
  local question="$1" prompt answer project line
  project="$(dirname "$PLAN_FILE")"
  prompt="$(cat "$REPO/tools/ask_prompt.txt")"
  prompt="${prompt//__QUESTION__/$question}"
  echo "?? $question"
  # Read-only and scoped to the project folder: the Read/Grep/Glob rules with ./**
  # stop it reading anything outside, and .env / the Onshape key are denied inside.
  answer="$( cd "$project" && claude -p "$prompt" \
      --allowedTools "Read(./**)" "Grep(./**)" "Glob(./**)" "WebSearch" "WebFetch" \
      --disallowedTools "Read(**/.env)" "Read(**/.env.*)" "Read(./Onshape key.docx)" \
    < /dev/null 2>&1 | head -c 4000 )"
  [ -n "$answer" ] || answer="Claude gaf geen antwoord."
  line="$(python3 -c 'import json,sys; from datetime import datetime; print(json.dumps({"tijd": datetime.now().isoformat(timespec="seconds"), "vraag": sys.argv[1], "antwoord": sys.argv[2]}, ensure_ascii=False))' "$question" "$answer")"
  ssh -n "$PI" "printf '%s\n' $(printf '%q' "$line") >> $PI_DIR/$ANSWERS" 2>/dev/null || true
  echo "   -> answered (${#answer} chars)"
}

ask_pass() {
  # Questions first: they are quick and someone is waiting for the answer.
  local fetched
  fetched="$(ssh "$PI" "test -s $PI_DIR/$QUESTIONS && cat $PI_DIR/$QUESTIONS && : > $PI_DIR/$QUESTIONS" 2>/dev/null)"
  [ -n "$fetched" ] || return 0
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    ask_one "$(printf '%s' "$line" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("vraag",""))' 2>/dev/null)"
  done <<< "$fetched"
}

pass() {
  # Crash-safe handover: the queue is moved to a local file BEFORE the Pi's copy
  # is cleared, and each line is removed only once it has been dealt with. Losing
  # the worker mid-pass then costs at most the task it was working on.
  local pending="$REPO/.local/pending.jsonl"
  mkdir -p "$REPO/.local"

  if [ ! -s "$pending" ]; then
    local fetched
    fetched="$(ssh "$PI" "test -s $PI_DIR/$QUEUE && cat $PI_DIR/$QUEUE" 2>/dev/null)"
    [ -n "$fetched" ] || return 0
    printf '%s\n' "$fetched" > "$pending"
    ssh "$PI" ": > $PI_DIR/$QUEUE" 2>/dev/null
  fi

  while [ -s "$pending" ]; do
    local line parsed task plan risk
    line="$(head -n 1 "$pending")"
    if [ -z "$line" ]; then
      tail -n +2 "$pending" > "$pending.tmp" && mv "$pending.tmp" "$pending"
      continue
    fi
    parsed="$(printf '%s' "$line" | python3 "$REPO/tools/improve_parse.py" 2>/dev/null)"
    task="$(sed -n 1p <<< "$parsed")"
    plan="$(sed -n 2p <<< "$parsed")"
    risk="$(sed -n 3p <<< "$parsed")"
    [ -n "$task" ] && run_one "$task" "$plan" "$risk"
    # Only now is it gone.
    tail -n +2 "$pending" > "$pending.tmp" && mv "$pending.tmp" "$pending"
  done
}

mkdir -p "$WORKTREES"
if [ "${1:-}" = "--watch" ]; then
  echo "watching $PI - Ctrl-C to stop"
  while true; do ask_pass; pass; sleep 20; done
else
  ask_pass; pass; echo "done"
fi
