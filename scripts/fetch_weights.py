#!/usr/bin/env python
"""Pre-download model weights referenced by config/pipeline.yaml into HF_HOME.

Run by bootstrap.sh so the first real job doesn't stall on a ~18GB download.
Safe to run repeatedly (huggingface_hub caches). Failures are non-fatal.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

CFG = Path(__file__).resolve().parent.parent / "config" / "pipeline.yaml"


def main() -> int:
    cfg = yaml.safe_load(CFG.read_text())
    models: list[str] = []

    prof = cfg["profiles"][cfg["llm"]["profile"]]
    models.append(prof["model"])

    qe = cfg.get("qe", {})
    if qe.get("enabled"):
        models.append(qe["model"])
        bt = qe.get("backtranslation", {})
        if bt.get("enabled"):
            models.append(bt["model"])

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("huggingface_hub not installed; skipping prefetch", file=sys.stderr)
        return 0

    rc = 0
    for repo in dict.fromkeys(models):  # dedupe, keep order
        try:
            print(f"[fetch_weights] downloading {repo} ...")
            snapshot_download(repo_id=repo)
        except Exception as e:  # noqa: BLE001 - prefetch is best-effort
            print(f"[fetch_weights] WARN could not fetch {repo}: {e}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
