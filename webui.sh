#!/usr/bin/env bash
# Launch the qc-translate web UI (upload / status / download).
# Binds 0.0.0.0:7860 so RunPod's built-in HTTP proxy exposes it at
#   https://<POD_ID>-7860.proxy.runpod.net
# NOTE: the proxy URL is reachable by anyone who has it (no login), and this handles client
# documents — treat the URL as a secret, or front it with your own auth if that matters.
set -uo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -f "$REPO_DIR/.env.runtime" ]] && source "$REPO_DIR/.env.runtime"
source "$REPO_DIR/scripts/runpod_env.sh" 2>/dev/null || true

PORT="${QC_UI_PORT:-7860}"
echo "qc-translate UI on 0.0.0.0:$PORT — RunPod proxy: https://<POD_ID>-$PORT.proxy.runpod.net"
exec python -m uvicorn qc_translate.webapp:app --host 0.0.0.0 --port "$PORT"
