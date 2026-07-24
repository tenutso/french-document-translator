#!/usr/bin/env bash
# Launch the qc-translate web UI (upload / status / download).
# Expose ONLY via an SSH tunnel or RunPod's authenticated proxy — it has no built-in auth
# and handles client documents. e.g. from your laptop:
#   ssh -L 8080:localhost:8080 <pod>    then open http://localhost:8080
set -uo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -f "$REPO_DIR/.env.runtime" ]] && source "$REPO_DIR/.env.runtime"
source "$REPO_DIR/scripts/runpod_env.sh" 2>/dev/null || true

PORT="${QC_UI_PORT:-8080}"
echo "qc-translate UI on http://127.0.0.1:$PORT  (Ctrl+C to stop)"
exec python -m uvicorn qc_translate.webapp:app --host 0.0.0.0 --port "$PORT"
