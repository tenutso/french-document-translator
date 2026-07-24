"""Shared data structures passed between pipeline stages."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


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
    tm_fuzzy: list[tuple[str, str, float]] = field(default_factory=list)  # (src, tgt, score)
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
