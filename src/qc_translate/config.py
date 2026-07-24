"""Load and access pipeline configuration (config/pipeline.yaml)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Repo root = two levels up from this file (src/qc_translate/config.py).
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "pipeline.yaml"


@dataclass
class Config:
    """Thin typed wrapper over the parsed YAML with a few resolved helpers."""

    raw: dict[str, Any]
    path: Path = field(default=DEFAULT_CONFIG)

    # --- convenience accessors ------------------------------------------------
    @property
    def profile(self) -> dict[str, Any]:
        return self.raw["profiles"][self.raw["llm"]["profile"]]

    @property
    def llm(self) -> dict[str, Any]:
        return self.raw["llm"]

    @property
    def language(self) -> dict[str, Any]:
        return self.raw["language"]

    @property
    def glossary(self) -> dict[str, Any]:
        return self.raw["glossary"]

    @property
    def qe(self) -> dict[str, Any]:
        return self.raw.get("qe", {})

    @property
    def tm(self) -> dict[str, Any]:
        return self.raw["tm"]

    @property
    def images(self) -> dict[str, Any]:
        return self.raw.get("images", {})

    def path_for(self, key: str) -> Path:
        """Resolve a path from the `paths` block, relative to the repo if needed."""
        val = self.raw["paths"][key]
        p = Path(val)
        return p if p.is_absolute() else (REPO_ROOT / p)

    def repo_path(self, rel: str) -> Path:
        """Resolve a repo-relative path (e.g. glossary/style_guide.md)."""
        p = Path(rel)
        return p if p.is_absolute() else (REPO_ROOT / p)

    @property
    def okapi_home(self) -> Path:
        return Path(os.environ.get("OKAPI_HOME", str(self.path_for("okapi_home"))))

    @property
    def tesseract_bin(self) -> str:
        return os.environ.get("TESSERACT_BIN", self.raw["paths"].get("tesseract_bin", "tesseract"))


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path) if path else DEFAULT_CONFIG
    raw = yaml.safe_load(p.read_text())
    return Config(raw=raw, path=p)
