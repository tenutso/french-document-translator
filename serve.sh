#!/usr/bin/env bash
# Start/stop the vLLM OpenAI-compatible server using the active profile in
# config/pipeline.yaml. Usage: bash serve.sh {start|stop|status|logs}
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$REPO_DIR/.env.runtime"
PID_FILE="/workspace/vllm.pid"
LOG_FILE="/workspace/vllm.log"

[[ -f "$ENV_FILE" ]] && source "$ENV_FILE" || { echo "Run bootstrap.sh first ($ENV_FILE missing)"; exit 1; }

cmd="${1:-start}"

read_cfg() {
  # Emit shell assignments for the active LLM profile from pipeline.yaml.
  python - "$REPO_DIR/config/pipeline.yaml" <<'PY'
import sys, yaml, shlex
cfg = yaml.safe_load(open(sys.argv[1]))
llm = cfg["llm"]; prof = cfg["profiles"][llm["profile"]]
host, port = "127.0.0.1", llm["base_url"].rsplit(":", 1)[1].split("/")[0]
out = {
    "MODEL": prof["model"], "DTYPE": prof.get("dtype", "auto"),
    "QUANT": prof.get("quantization") or "",
    "GPU_UTIL": prof.get("gpu_memory_utilization", 0.9),
    "MAXLEN": prof.get("max_model_len", 8192),
    "TP": prof.get("tensor_parallel_size", 1), "PORT": port,
}
for k, v in out.items():
    print(f"{k}={shlex.quote(str(v))}")
PY
}

case "$cmd" in
  start)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "vLLM already running (pid $(cat "$PID_FILE"))"; exit 0
    fi
    eval "$(read_cfg)"
    QUANT_ARG=(); [[ -n "$QUANT" ]] && QUANT_ARG=(--quantization "$QUANT")
    echo "Starting vLLM: $MODEL (port $PORT, tp=$TP, dtype=$DTYPE)"
    nohup python -m vllm.entrypoints.openai.api_server \
      --model "$MODEL" --dtype "$DTYPE" "${QUANT_ARG[@]}" \
      --gpu-memory-utilization "$GPU_UTIL" --max-model-len "$MAXLEN" \
      --tensor-parallel-size "$TP" --host 127.0.0.1 --port "$PORT" \
      > "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "pid $(cat "$PID_FILE") — tail logs: bash serve.sh logs"
    ;;
  stop)
    [[ -f "$PID_FILE" ]] && kill "$(cat "$PID_FILE")" 2>/dev/null && rm -f "$PID_FILE" && echo "stopped" || echo "not running"
    ;;
  status)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "running (pid $(cat "$PID_FILE"))"
    else echo "not running"; fi
    ;;
  logs) tail -f "$LOG_FILE" ;;
  *) echo "usage: bash serve.sh {start|stop|status|logs}"; exit 1 ;;
esac
