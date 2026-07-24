"""Reviewer handoff: package deliverables and fold corrections back into the TM.

Workflow (Word path, recommended for most reviewers):
  1. `qc-translate package <job>`  -> a zip the reviewer opens (French .docx + reports + guide)
  2. reviewer edits the .docx in Word (Track Changes), accepts changes, sends it back
  3. `qc-translate import-review <reviewed.docx> --job <job>`  -> updates the TM so future
     documents reuse the approved wording; the reviewed .docx is itself the final deliverable

OmegaT path: reviewer edits the XLIFF/omegat_project; run `merge` for the final .docx and
`import-review <reviewed.xlf> --job <job>` to update the TM.
"""
from __future__ import annotations

import shutil
import tempfile
import zipfile
from collections import defaultdict, deque
from difflib import SequenceMatcher
from pathlib import Path

from . import okapi
from .config import Config
from .tm import TranslationMemory, plain
from .xliff import read_sources, read_targets

REVIEW_INSTRUCTIONS = """# French review — instructions

You have received a machine-translated **Quebec French (fr-CA)** version of the manual to
review and polish. It is close to final; your job is to correct wording, terminology, and
anything that reads awkwardly.

## Files in this package
- `*.fr-CA.draft.docx` — the French document. **This is what you edit.**
- `qa_report.html` — segments the system flagged, worst first (open in a browser).
  Start here: the lowest quality-estimate scores and flagged terms are most likely to need
  attention. Note: some "glossary" flags are false positives (correct conjugations).
- `image_report.html` — embedded images. Ten contain English text and need to be
  recreated by a designer; each lists the English text and a suggested French rendering.
- `translated.xlf` — the bilingual source/target file (only needed for CAT-tool review).

## How to review (Word)
1. Open the `.fr-CA.draft.docx` in Microsoft Word.
2. Turn on **Review → Track Changes** so your edits are visible.
3. When prompted to update fields (or press Ctrl+A then F9), let it update so the **Table of
   Contents** rebuilds in French.
4. Correct the text. Spell-check runs in Canadian French.
5. Save and send the file back.

## Terminology
Use the CAPS Brand Lexicon and inclusive French (median dot, e.g. conférencier·ère). Keep
CAPS/CSP/HoF/GSF in English.

## After review
The team runs `qc-translate import-review <your-file>.docx` so your approved wording is
remembered and reused in future documents — please do not change the document's structure
(don't add/remove paragraphs) so corrections align cleanly.
"""


def package(cfg: Config, job_dir: str | Path, out_zip: str | Path | None = None) -> tuple[Path, list[Path]]:
    """Zip the reviewer-facing deliverables + instructions, and refresh the TMX export."""
    job_dir = Path(job_dir)
    included: list[Path] = []
    fr = sorted(job_dir.glob("*.fr-CA.draft.docx"))
    if fr:
        included.append(fr[0])
    for name in ("qa_report.html", "image_report.html", "translated.xlf"):
        p = job_dir / name
        if p.exists():
            included.append(p)

    # Refresh the durable TMX export alongside the package.
    tm = TranslationMemory(cfg.tm["db"])
    tmx = tm.export_tmx(cfg.tm["tmx_export"], cfg.language["source"], cfg.language["target"])
    tm.close()

    out_zip = Path(out_zip) if out_zip else job_dir / f"{job_dir.name}_review_package.zip"
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for f in included:
            z.write(f, f.name)
        z.writestr("REVIEW_INSTRUCTIONS.md", REVIEW_INSTRUCTIONS)
    return out_zip, included + [tmx]


def import_review(cfg: Config, reviewed: str | Path, job_dir: str | Path) -> tuple[int, int]:
    """Fold a reviewer's corrections into the TM. Returns (updated, total_pairs).

    Accepts the reviewed French `.docx` (aligned positionally to the English sources) or a
    reviewed XLIFF (aligned by trans-unit id).
    """
    reviewed = Path(reviewed)
    job_dir = Path(job_dir)
    src_units = dict(read_sources(job_dir / "source.docx.xlf"))   # id -> english inner-xml

    if reviewed.suffix.lower() in (".xlf", ".xliff"):
        # XLIFF path: id-based, exact — the reliable route (OmegaT/CAT).
        targets = read_targets(reviewed)
        pairs = [(en, targets[uid]) for uid, en in src_units.items() if uid in targets]
        total = len(pairs)
    elif reviewed.suffix.lower() == ".docx":
        pairs, total = _align_docx(cfg, reviewed, job_dir, src_units)
    else:
        raise ValueError("reviewed file must be .docx or .xlf/.xliff")

    tm = TranslationMemory(cfg.tm["db"])
    updated = 0
    for en, fr in pairs:
        if plain(fr) and plain(fr) != plain(en):
            tm.upsert(en, fr)
            updated += 1
    tm.export_tmx(cfg.tm["tmx_export"], cfg.language["source"], cfg.language["target"])
    tm.close()
    return updated, total


def _align_docx(cfg: Config, reviewed: Path, job_dir: Path,
                src_units: dict[str, str]) -> tuple[list[tuple[str, str]], int]:
    """Align a reviewed French .docx to English sources without relying on segment counts.

    Re-extracting French under the English SRX can drift by a few segments, so strict
    positional alignment is unsafe. Instead: exact-match reviewed segments to the machine
    French (unchanged segments) to consume ids, then fuzzy-match the reviewer's edits to
    the remaining machine-French segments. Only changed segments carry new info for the TM.
    """
    machine_fr = read_targets(job_dir / "translated.xlf")         # id -> machine French
    work = Path(tempfile.mkdtemp(prefix="review_", dir=str(job_dir)))
    try:
        rev_xlf = okapi.extract(cfg, reviewed, work)
        reviewed_fr = [fr for _, fr in read_sources(rev_xlf)]     # French text in <source>
    finally:
        shutil.rmtree(work, ignore_errors=True)

    by_plain: dict[str, deque] = defaultdict(deque)
    for uid, mf in machine_fr.items():
        by_plain[plain(mf)].append(uid)

    used: set[str] = set()
    edited: list[str] = []
    for fr in reviewed_fr:
        q = by_plain.get(plain(fr))
        if q:
            used.add(q.popleft())        # unchanged segment — consume its id
        else:
            edited.append(fr)            # reviewer changed this one

    remaining = [(uid, plain(machine_fr[uid])) for uid in machine_fr if uid not in used]
    pairs: list[tuple[str, str]] = []
    for fr in edited:
        pf = plain(fr)
        best_uid, best = None, 0.0
        for uid, mplain in remaining:
            r = SequenceMatcher(None, pf, mplain).ratio()
            if r > best:
                best, best_uid = r, uid
        if best_uid is not None and best >= 0.6:
            pairs.append((src_units[best_uid], fr))
            remaining = [(u, m) for u, m in remaining if u != best_uid]
    return pairs, len(reviewed_fr)
