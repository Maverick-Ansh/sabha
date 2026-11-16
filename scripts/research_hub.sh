#!/usr/bin/env bash
# Start the Sabha hub on the box next to the running research daemon and publish the phone link.
#
#   bash scripts/research_hub.sh          start or restart the hub (detached), write the link file
#   bash scripts/research_hub.sh stop     stop the hub
#   bash scripts/research_hub.sh link     print the saved link
#
# The hub only reads the research data repo and appends commands to missions/<id>/state/inbox.jsonl,
# so starting, stopping or restarting it never touches the daemon or the model servers.
#
# What it sets up
#   log        /kaggle/working/logs/hub.log
#   link       /kaggle/working/sabha-research/box_logs/hub_link.txt   (the data repo is private and the
#              daemon pushes it every 10 minutes, so the link reaches the laptop and the phone through git)
#   token      fixed: env SABHA_TOKEN, else /kaggle/working/.sabha_token (made once). A restart keeps the token.
#              The host part of a trycloudflare link is new on every start; the link file always holds the current one.
#              For a host that never changes set TUNNEL=ngrok with NGROK_AUTHTOKEN and NGROK_DOMAIN.
#   models     deep (127.0.0.1:8001) and reader (127.0.0.1:8002) registered as plain base_url models, thinking off
# Last line printed is HUB_OK <link> or HUB_FAIL <why>.
set -u
CODE=${CODE:-/kaggle/working/sabha}
RESEARCH=${SABHA_RESEARCH_REPO:-/kaggle/working/sabha-research}
LOGS=${LOGS:-/kaggle/working/logs}
DATA=${HUB_DATA:-/kaggle/working/sabha_hub_data}
PORT=${PORT:-8787}
TUNNEL=${TUNNEL:-cloudflared}
PY=${PY:-python3}
TOKEN_FILE=${TOKEN_FILE:-/kaggle/working/.sabha_token}
PIDF="$LOGS/hub.pid"
LINKF="$RESEARCH/box_logs/hub_link.txt"
LOG="$LOGS/hub.log"

say() { echo "[hub $(date +%H:%M:%S)] $*"; }

stop_hub() {
  local pid=""
  [ -f "$PIDF" ] && pid=$(cat "$PIDF" 2>/dev/null)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    say "stopping hub $pid"
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null   # the whole group: hub and its tunnel
    for _ in $(seq 1 20); do
      if kill -0 "$pid" 2>/dev/null; then sleep 1; else break; fi
    done
    if kill -0 "$pid" 2>/dev/null; then kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null; fi
  fi
  # a hub this script did not record (never the daemon: that one runs "-m sabha.research.run")
  if pgrep -f -- "-m sabha --host 127.0.0.1 --port $PORT " >/dev/null 2>&1; then
    pkill -TERM -f -- "-m sabha --host 127.0.0.1 --port $PORT " 2>/dev/null
    sleep 5
    pkill -KILL -f -- "-m sabha --host 127.0.0.1 --port $PORT " 2>/dev/null
  fi
  rm -f "$PIDF"
}

case "${1:-start}" in
  stop) stop_hub; echo "HUB_STOPPED"; exit 0 ;;
  link) cat "$LINKF" 2>/dev/null || echo "no link yet"; exit 0 ;;
esac

mkdir -p "$LOGS" "$DATA" "$RESEARCH/box_logs"
[ -d "$CODE/sabha" ] || { echo "HUB_FAIL no hub code at $CODE"; exit 1; }
[ -d "$RESEARCH" ] || { echo "HUB_FAIL no research repo at $RESEARCH"; exit 1; }

# newest hub code (the daemon keeps running the code it already loaded; it picks up new code on its own restart)
if [ "${PULL:-1}" = 1 ]; then
  (cd "$CODE" && git pull -q --autostash 2>&1 | tail -2; say "code: $(git log --oneline -1 2>/dev/null)")
fi
"$PY" -c "import aiohttp" 2>/dev/null || { say "installing aiohttp"; "$PY" -m pip install -q aiohttp || { echo "HUB_FAIL pip install aiohttp"; exit 1; }; }

if [ -z "${SABHA_TOKEN:-}" ]; then
  [ -s "$TOKEN_FILE" ] || "$PY" -c "import secrets; print(secrets.token_urlsafe(18))" > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE" 2>/dev/null
  SABHA_TOKEN=$(cat "$TOKEN_FILE")
fi
export SABHA_TOKEN SABHA_RESEARCH_REPO="$RESEARCH"

stop_hub
[ -f "$LOG" ] && mv -f "$LOG" "$LOG.prev"
cd "$CODE" || { echo "HUB_FAIL cd $CODE"; exit 1; }
setsid nohup "$PY" -m sabha --host 127.0.0.1 --port "$PORT" --data "$DATA" --tunnel "$TUNNEL" \
  --session-hours "${SESSION_HOURS:-12}" --research-repo "$RESEARCH" \
  --model "deep=${DEEP_URL:-http://127.0.0.1:8001/v1}" --model "reader=${READER_URL:-http://127.0.0.1:8002/v1}" --no-think \
  > "$LOG" 2>&1 < /dev/null &
echo $! > "$PIDF"
sleep 2
HUBPID=$(pgrep -o -f -- "-m sabha --host 127.0.0.1 --port $PORT " 2>/dev/null)
[ -n "$HUBPID" ] && echo "$HUBPID" > "$PIDF"
say "hub started, pid $(cat "$PIDF"), log $LOG"

# wait for the line "Sabha link: ..." (the tunnel can take a minute; the first start also downloads cloudflared)
LINK=""
for _ in $(seq 1 100); do
  LINK=$(grep -m1 '^Sabha link: ' "$LOG" 2>/dev/null | sed 's/^Sabha link: //')
  [ -n "$LINK" ] && break
  kill -0 "$(cat "$PIDF")" 2>/dev/null || { tail -20 "$LOG"; echo "HUB_FAIL the hub exited, see $LOG"; exit 1; }
  sleep 2
done
[ -n "$LINK" ] || { tail -20 "$LOG"; echo "HUB_FAIL no link after 200 s, see $LOG"; exit 1; }
case "$LINK" in
  http://127.0.0.1*|http://localhost*) say "WARNING: the tunnel did not come up; this link works only on the box"; grep -m1 'Tunnel failed' "$LOG" ;;
esac

{
  echo "$LINK"
  echo
  echo "Sabha Research on the phone. Open the first line. Written $(date -u +%Y-%m-%dT%H:%M:%SZ) by scripts/research_hub.sh."
  echo "The token after #token= stays the same across restarts; the host changes when the hub restarts (trycloudflare)."
  echo "Keep this link private: it controls the research daemon."
} > "$LINKF"
say "link written to $LINKF"
curl -s -m 8 -H "Authorization: Bearer $SABHA_TOKEN" "http://127.0.0.1:$PORT/api/research" 2>/dev/null \
  | "$PY" -c "import json,sys; d=json.load(sys.stdin); m=d.get('mission') or {}; print('[hub] research:', 'available' if d.get('available') else 'NOT available', '| mission', m.get('id'), '| agents', len(d.get('agents') or []), '| daemon alive', (d.get('daemon') or {}).get('alive'))" 2>/dev/null \
  || say "could not read /api/research yet"
echo "HUB_OK $LINK"
