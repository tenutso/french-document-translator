"""Make RunPod-injected environment variables available to the pipeline.

RunPod injects template/secret env vars into the container's PID 1, but a freshly
spawned shell (or the process running this CLI) may not inherit them. This module
back-fills any missing keys from PID 1's environment and from an optional `.env` file,
so `qc-translate` "just works" regardless of how the shell was started.

huggingface_hub reads HF_TOKEN / HUGGING_FACE_HUB_TOKEN automatically, so once those are
in os.environ, gated downloads (e.g. CometKiwi) succeed with no further code.
"""
from __future__ import annotations

import os
from pathlib import Path

# Secrets/config the pipeline may need from the RunPod environment.
WANTED = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HF_HOME", "GITHUB_TOKEN", "GH_TOKEN")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _from_pid1() -> dict[str, str]:
    """Parse PID 1's environment (best-effort; requires readable /proc/1/environ)."""
    try:
        raw = Path("/proc/1/environ").read_bytes()
    except (FileNotFoundError, PermissionError, OSError):
        return {}
    out: dict[str, str] = {}
    for chunk in raw.split(b"\0"):
        if b"=" in chunk:
            k, _, v = chunk.partition(b"=")
            out[k.decode(errors="replace")] = v.decode(errors="replace")
    return out


# .env is looked for in the repo root and the /workspace volume root (RunPod's natural
# spot), plus the current directory. Earlier paths win.
DOTENV_PATHS = (REPO_ROOT / ".env", Path("/workspace/.env"), Path.cwd() / ".env")


def _from_dotenv() -> dict[str, str]:
    """Read simple KEY=VALUE lines from the first existing .env (none is fine)."""
    out: dict[str, str] = {}
    seen: set[Path] = set()
    for env_file in DOTENV_PATHS:
        if env_file in seen or not env_file.exists():
            continue
        seen.add(env_file)
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return out


def load_injected_secrets() -> None:
    """Back-fill WANTED vars into os.environ from .env then PID 1 (current shell wins)."""
    dotenv = _from_dotenv()
    pid1 = _from_pid1()
    for key in WANTED:
        if os.environ.get(key):
            continue  # an explicitly-set value in this shell always wins
        val = dotenv.get(key) or pid1.get(key)
        if val:
            os.environ[key] = val

    # Normalise the two HF token aliases so whichever is set satisfies huggingface_hub.
    hf = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if hf:
        os.environ.setdefault("HF_TOKEN", hf)
        os.environ.setdefault("HUGGING_FACE_HUB_TOKEN", hf)
