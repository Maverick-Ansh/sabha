#!/usr/bin/env bash
# Keep one vLLM server alive: launch, probe /v1/models every 30 s, relaunch on death or 3 missed probes.
# usage: serve.sh NAME GPU PORT MODEL UTIL MAXLEN MAXSEQS
# Run it detached (setsid). Logs: /kaggle/working/logs/NAME.vllm.log
NAME=$1; GPU=$2; PORT=$3; MODEL=$4; UTIL=$5; MAXLEN=$6; SEQS=$7
LOG=/kaggle/working/logs/$NAME.vllm.log
VLLM=${VLLM:-/opt/vllm/bin/vllm}
say() { echo "[serve $NAME $(date +%H:%M:%S)] $*"; }
while true; do
  say "launch $MODEL on gpu $GPU port $PORT"
  CUDA_VISIBLE_DEVICES=$GPU VLLM_ATTENTION_BACKEND=TRITON_ATTN setsid "$VLLM" serve "$MODEL" \
    --served-model-name "$NAME" --port "$PORT" --dtype float16 --max-num-seqs "$SEQS" \
    --max-model-len "$MAXLEN" --gpu-memory-utilization "$UTIL" --enable-prefix-caching \
    --reasoning-parser qwen3 --limit-mm-per-prompt '{"image":0,"video":0}' >>"$LOG" 2>&1 &
  PID=$!
  up=0; miss=0; t0=$(date +%s)
  while kill -0 "$PID" 2>/dev/null; do
    sleep 30
    if curl -sf -m 10 "http://127.0.0.1:$PORT/v1/models" >/dev/null; then
      [ $up = 0 ] && say "UP after $(( $(date +%s) - t0 )) s"; up=1; miss=0
      echo "$(date +%s) up" > "/tmp/serve_$NAME.health"
    else
      if [ $up = 1 ]; then miss=$((miss+1)); fi
      if [ $up = 0 ] && [ $(( $(date +%s) - t0 )) -gt 1500 ]; then miss=3; fi
      if [ $miss -ge 3 ]; then say "unhealthy, killing $PID"; kill -9 -- -"$PID" 2>/dev/null; kill -9 "$PID" 2>/dev/null; break; fi
    fi
  done
  wait "$PID" 2>/dev/null
  say "exited rc=$?; relaunch in 20 s"; echo "$(date +%s) down" > "/tmp/serve_$NAME.health"
  sleep 20
done
