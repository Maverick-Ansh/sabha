#!/usr/bin/env bash
# Keep one vLLM server alive: launch, probe /v1/models every 30 s, relaunch on death or 3 missed probes.
# usage: serve.sh NAME GPU PORT MODEL UTIL MAXLEN MAXSEQS
# Run it detached (setsid). Logs: $SABHA_LOGS/NAME.vllm.log (default /kaggle/working/logs)
if [ "$1" = stop ]; then  # serve.sh stop NAME: kill the server's whole process group (EngineCore children too)
  P=$(cat "/tmp/serve_$2.pid" 2>/dev/null); [ -n "$P" ] && kill -9 -- -"$P" 2>/dev/null; rm -f "/tmp/serve_$2.pid" "/tmp/serve_$2.health"; exit 0
fi
NAME=$1; GPU=$2; PORT=$3; MODEL=$4; UTIL=$5; MAXLEN=$6; SEQS=$7
LOGDIR=${SABHA_LOGS:-/kaggle/working/logs}; mkdir -p "$LOGDIR"; LOG=$LOGDIR/$NAME.vllm.log
TOOLP=${SABHA_TOOL_PARSER-qwen3_xml}  # Qwen3.5 tool calls (Work mode / dsh); with hermes they vanish silently. Empty = off
TOOLARGS=(); [ -n "$TOOLP" ] && TOOLARGS=(--enable-auto-tool-choice --tool-call-parser "$TOOLP")
REASP=${SABHA_REASONING_PARSER-qwen3}  # empty = off (a model family without a known parser)
EXTRA=(); [ -n "$REASP" ] && EXTRA+=(--reasoning-parser "$REASP")
[ "${SABHA_MM:-1}" = 1 ] && EXTRA+=(--limit-mm-per-prompt '{"image":0,"video":0}')  # multimodal checkpoints: text only
TP=${SABHA_TP:-1}; [ "$TP" -gt 1 ] && EXTRA+=(--tensor-parallel-size "$TP")       # GPU may be a list: 0,1
DTYPE=${SABHA_DTYPE:-float16}                                                    # T4 has no bf16; A100 and newer: auto
ATTN=${SABHA_ATTN-}; [ -z "$ATTN" ] && [ "$DTYPE" = float16 ] && ATTN=TRITON_ATTN  # sm75: FlexAttention is ~5x slower
VLLM=${VLLM:-/opt/vllm/bin/vllm}
say() { echo "[serve $NAME $(date +%H:%M:%S)] $*"; }
while true; do
  OLD=$(cat "/tmp/serve_$NAME.pid" 2>/dev/null); [ -n "$OLD" ] && kill -9 -- -"$OLD" 2>/dev/null && sleep 5
  say "launch $MODEL on gpu $GPU port $PORT (tp $TP, $DTYPE)"
  ENVV=(CUDA_VISIBLE_DEVICES="$GPU"); [ -n "$ATTN" ] && ENVV+=(VLLM_ATTENTION_BACKEND="$ATTN")
  env "${ENVV[@]}" setsid "$VLLM" serve "$MODEL" \
    --served-model-name "$NAME" --port "$PORT" --dtype "$DTYPE" --max-num-seqs "$SEQS" \
    --max-model-len "$MAXLEN" --gpu-memory-utilization "$UTIL" --enable-prefix-caching \
    "${EXTRA[@]}" "${TOOLARGS[@]}" >>"$LOG" 2>&1 &
  PID=$!
  echo "$PID" > "/tmp/serve_$NAME.pid"
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
