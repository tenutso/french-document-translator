#!/usr/bin/env bash
# Source this to back-fill RunPod-injected env vars into the current shell.
#   source scripts/runpod_env.sh
# Order of precedence: current shell (unchanged) > repo .env > PID 1 (container boot env).
# Mirrors src/qc_translate/runpod_env.py for the bash side (serve.sh / bootstrap.sh).

_qc_repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 1) optional .env (KEY=VALUE lines; not committed) — repo root or /workspace volume root
for _qc_envf in "$_qc_repo/.env" "/workspace/.env"; do
  if [[ -f "$_qc_envf" ]]; then
    set -a; # shellcheck disable=SC1090
    source "$_qc_envf"; set +a
    break
  fi
done

# 2) PID 1 fallback for anything still missing (RunPod injects into the boot process)
_qc_import_pid1() {
  [[ -r /proc/1/environ ]] || return 0
  local k v
  for k in "$@"; do
    if [[ -z "${!k:-}" ]]; then
      v="$(tr '\0' '\n' < /proc/1/environ | grep -m1 "^${k}=" | cut -d= -f2- || true)"
      [[ -n "$v" ]] && export "$k=$v"
    fi
  done
}
_qc_import_pid1 HF_TOKEN HUGGING_FACE_HUB_TOKEN HF_HOME GITHUB_TOKEN GH_TOKEN

# 3) normalise the two HF token aliases
if [[ -n "${HF_TOKEN:-}${HUGGING_FACE_HUB_TOKEN:-}" ]]; then
  export HF_TOKEN="${HF_TOKEN:-$HUGGING_FACE_HUB_TOKEN}"
  export HUGGING_FACE_HUB_TOKEN="${HUGGING_FACE_HUB_TOKEN:-$HF_TOKEN}"
fi
