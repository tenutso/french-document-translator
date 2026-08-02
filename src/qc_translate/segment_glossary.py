"""Parse the TBX glossary and pre-match terms against each source segment.

Produces per-segment {source_term: required_target} hints that are injected into the
translation prompt and later checked by QA.
"""
from __future__ import annotations

import re
from pathlib import Path

from lxml import etree

from .models import Segment
from .tm import plain


class Glossary:
    def __init__(self, entries: list[tuple[str, str]], do_not_translate: list[str]):
        # entries: (source_term, target_term); longest source first so multi-word
        # terms win over their sub-words during matching.
        self.entries = sorted(entries, key=lambda e: len(e[0]), reverse=True)
        self.do_not_translate = do_not_translate
        self._compiled = [
            (re.compile(rf"\b{re.escape(src)}\b", re.IGNORECASE), src, tgt)
            for src, tgt in self.entries
        ]

    @classmethod
    def load(cls, tbx_path: str | Path) -> "Glossary":
        tree = etree.parse(str(tbx_path))
        # TBX-Basic has no default namespace in our sample; handle both cases.
        entries: list[tuple[str, str]] = []
        dnt: list[str] = []
        for te in tree.iter("{*}termEntry"):
            src_term = tgt_term = None
            for ls in te.iter("{*}langSet"):
                lang = ls.get("{http://www.w3.org/XML/1998/namespace}lang", "")
                term_el = ls.find(".//{*}term")
                if term_el is None or not term_el.text:
                    continue
                if lang.startswith("en"):
                    src_term = term_el.text.strip()
                elif lang.startswith("fr"):
                    tgt_term = term_el.text.strip()
            dnt_flag = any(
                (d.get("type") == "doNotTranslate" and (d.text or "").strip().lower() == "yes")
                for d in te.iter("{*}descrip")
            )
            if src_term and dnt_flag:
                dnt.append(src_term)
            elif src_term and tgt_term:
                entries.append((src_term, tgt_term))
        return cls(entries, dnt)

    def match(self, source_xml: str) -> dict[str, str]:
        """Return {source_term: required_target} for terms present in the segment.

        Terms are tried longest-source-first (see __init__); a shorter term whose match
        falls entirely inside an already-claimed span is skipped, so e.g. "CAPS Convention"
        doesn't also fire standalone "CAPS" and "Convention" hits for the same words.
        """
        text = plain(source_xml)
        hits: dict[str, str] = {}
        claimed: list[tuple[int, int]] = []
        for pattern, src, tgt in self._compiled:
            m = pattern.search(text)
            if not m:
                continue
            start, end = m.span()
            if any(start >= cs and end <= ce for cs, ce in claimed):
                continue
            hits[src] = tgt
            claimed.append((start, end))
        return hits

    def annotate(self, segments: list[Segment]) -> None:
        for seg in segments:
            seg.glossary_hits = self.match(seg.source_xml)
