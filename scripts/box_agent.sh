#!/usr/bin/env bash
# Box agent: runs each new box_cmd/*.sh from origin/main exactly once.
# The laptop drives the box by pushing a script, so nothing depends on a live notebook cell.
# Output of each command lands in the data repo (box_logs/), which the research daemon pushes every 10 min.
REPO=${REPO:-/kaggle/working/sabha}
DONE=/kaggle/working/.box_cmd_done
OUT=/kaggle/working/sabha-research/box_logs
mkdir -p "$DONE" "$OUT"
# never re-run commands that already exist when the agent starts (a fresh box would replay old ones)
cd "$REPO" && git fetch -q origin 2>/dev/null
for f in $(git ls-tree --name-only origin/main box_cmd/ 2>/dev/null | grep '\.sh$'); do touch "$DONE/$(basename "$f" .sh)"; done
while true; do
  cd "$REPO" && git fetch -q origin 2>/dev/null
  for f in $(git ls-tree --name-only origin/main box_cmd/ 2>/dev/null | grep '\.sh$'); do
    n=$(basename "$f" .sh)
    [ -e "$DONE/$n" ] && continue
    touch "$DONE/$n"
    git show "origin/main:$f" > "/tmp/box_cmd_$n.sh"
    echo "[box_agent $(date +%H:%M:%S)] run $n"
    (cd "$REPO" && setsid bash "/tmp/box_cmd_$n.sh" > "$OUT/$n.log" 2>&1; echo "BOX_CMD_EXIT $?" >> "$OUT/$n.log") &
  done
  sleep 60
done
