"""Shared data structures passed between pipeline stages."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# How a segment relates to what the translation memory already knows — the basis of the
# revision report. Set in translate_segments from the TM lookup it already performs.
UNCHANGED_APPROVED = "unchanged-approved"   # exact match on wording a human signed off
UNCHANGED_MT = "unchanged-mt"               # exact match, but only machine output so far
CHANGED = "changed"                         # close match to a known source; English moved
NEW = "new"                                 # nothing comparable in the TM

VERSION_STATUSES = (UNCHANGED_APPROVED, UNCHANGED_MT, CHANGED, NEW)

# The XLIFF 1.2 <target state=""> each status should carry. These are standard values that
# OmegaT, Smartcat and memoQ all honour, so a reviewer's CAT tool greys out settled segments
# on import — they get the revision marking without opening a separate report.
STATE_BY_STATUS = {
    UNCHANGED_APPROVED: "signed-off",
    UNCHANGED_MT: "translated",
    CHANGED: "needs-review-translation",
    NEW: "needs-review-translation",
}


@dataclass
class Segment:
    """One translatable unit extracted from the XLIFF.

    `source_xml` / `target_xml` hold the *inner XML* of the XLIFF <source>/<target>,
    so inline formatting codes (<g>, <x/>, <bpt/>, <ph/> ...) are preserved verbatim
    and must be reproduced by the translator.
    """

    unit_id: str
    source_xml: str
    target_xml: Optional[str] = None
    # Glossary hints matched in the source: source_term -> required target_term.
    glossary_hits: dict[str, str] = field(default_factory=dict)
    # TM reuse: exact match reused verbatim; fuzzy match offered as reference.
    tm_exact: Optional[str] = None
    # (src, tgt, score, origin) — origin is 'approved' or 'mt', see tm.TranslationMemory.
    tm_fuzzy: list[tuple[str, str, float, str]] = field(default_factory=list)
    # Provenance of whichever TM entry backed this segment, if any.
    tm_origin: Optional[str] = None
    # One of VERSION_STATUSES; drives changes.html and the XLIFF state marking.
    version_status: Optional[str] = None
    # Previous French for a `changed` segment, shown side by side in changes.html.
    previous_target: Optional[str] = None
    # Previous English, when a previous job was supplied via `run --previous`.
    previous_source: Optional[str] = None
    # QA results.
    qe_score: Optional[float] = None
    qa_flags: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return bool(self.qa_flags)


@dataclass
class ImageInfo:
    """An embedded image and its context for the image report."""

    media_path: str            # e.g. word/media/image3.png
    rel_id: str                # relationship id (rId..)
    location: str              # human-readable anchor (nearest heading / section)
    ocr_text: str = ""
    has_text: bool = False
    suggested_fr: str = ""
    requires_recreation: bool = False
    thumbnail_data_uri: str = ""
