"""Emit an OmegaT project for the human review pass.

The machine-translated XLIFF is placed in source/ (OmegaT's XLIFF filter shows the
existing <target> as a pre-filled translation), the TM is attached as leverage, and the
glossary is exported as OmegaT's tab-separated format.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from lxml import etree

from .config import Config
from .segment_glossary import Glossary

_PROJECT = """<?xml version="1.0" encoding="UTF-8"?>
<omegat><project version="1.0">
 <source_dir>source/</source_dir>
 <target_dir>target/</target_dir>
 <tm_dir>tm/</tm_dir>
 <glossary_dir>glossary/</glossary_dir>
 <glossary_file>glossary/glossary.txt</glossary_file>
 <dictionary_dir>dictionary/</dictionary_dir>
 <source_lang>{src}</source_lang>
 <target_lang>{tgt}</target_lang>
 <sentence_seg>true</sentence_seg>
</project></omegat>
"""


def emit(cfg: Config, translated_xliff: str | Path, tmx: str | Path | None,
         project_dir: str | Path) -> Path:
    proj = Path(project_dir)
    for sub in ("source", "target", "tm", "glossary", "dictionary", "omegat"):
        (proj / sub).mkdir(parents=True, exist_ok=True)

    (proj / "omegat.project").write_text(
        _PROJECT.format(src=cfg.language["source"], tgt=cfg.language["target"]),
        encoding="utf-8",
    )
    shutil.copy2(translated_xliff, proj / "source" / Path(translated_xliff).name)
    if tmx and Path(tmx).exists():
        shutil.copy2(tmx, proj / "tm" / Path(tmx).name)

    _write_glossary(cfg, proj / "glossary" / "glossary.txt")
    return proj


def _write_glossary(cfg: Config, out: Path) -> None:
    tbx = cfg.repo_path(cfg.glossary["tbx"])
    if not tbx.exists():
        out.write_text("", encoding="utf-8")
        return
    gloss = Glossary.load(tbx)
    lines = [f"{src}\t{tgt}\t(brand/OQLF)" for src, tgt in gloss.entries]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
