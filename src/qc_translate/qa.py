"""Automated QA over translated segments.

Programmatic checks always run (fast, no model). CometKiwi reference-free QE runs if
enabled and installed; it scores every segment 0-1 so the reviewer works worst-first.
"""
from __future__ import annotations

import re
from collections import Counter

from .config import Config
from .models import Segment
from .tm import plain
from .xliff import inline_code_ids

_NUM = re.compile(r"\d[\d.,\s]*")


def _numbers(text: str) -> Counter:
    return Counter(re.sub(r"\s", "", n) for n in _NUM.findall(text))


def check_segment(seg: Segment, cfg: Config) -> None:
    """Run all programmatic checks, appending to seg.qa_flags."""
    src, tgt = seg.source_xml, seg.target_xml or ""

    # 1. Empty / untranslated.
    if not plain(tgt):
        seg.qa_flags.append("empty_target")
        return
    if plain(tgt) == plain(src) and len(plain(src)) > 3:
        seg.qa_flags.append("untranslated")

    # 2. Inline formatting codes must match exactly (order + multiplicity).
    if inline_code_ids(src) != inline_code_ids(tgt):
        seg.qa_flags.append("tag_mismatch")

    # 3. Numbers preserved.
    if _numbers(plain(src)) != _numbers(plain(tgt)):
        seg.qa_flags.append("number_mismatch")

    # 4. Glossary compliance: each required target term must appear.
    if cfg.glossary.get("enforce", True):
        low = plain(tgt).lower()
        for src_term, tgt_term in seg.glossary_hits.items():
            if tgt_term.lower() not in low:
                seg.qa_flags.append(f"glossary_miss:{src_term}->{tgt_term}")

    # 5. Length anomaly (FR is ~15-20% longer than EN; flag extremes).
    s_len, t_len = len(plain(src)), len(plain(tgt))
    if s_len >= 20 and (t_len < 0.5 * s_len or t_len > 2.5 * s_len):
        seg.qa_flags.append("length_anomaly")


def run_checks(cfg: Config, segments: list[Segment]) -> None:
    for seg in segments:
        check_segment(seg, cfg)


def run_quality_estimation(cfg: Config, segments: list[Segment]) -> None:
    """Score each segment with CometKiwi (reference-free). Best-effort/optional."""
    qe = cfg.qe
    if not qe.get("enabled"):
        return
    try:
        from comet import download_model, load_from_checkpoint
    except ImportError:
        for seg in segments:
            seg.qa_flags.append("qe_unavailable")
        return

    model_path = download_model(qe["model"])
    model = load_from_checkpoint(model_path)
    data = [{"src": plain(s.source_xml), "mt": plain(s.target_xml or "")} for s in segments]
    scores = model.predict(
        data, batch_size=qe.get("batch_size", 16), gpus=1, progress_bar=False
    )["scores"]

    flag_below = qe.get("flag_below", 0.75)
    for seg, score in zip(segments, scores):
        seg.qe_score = float(score)
        if score < flag_below:
            seg.qa_flags.append(f"low_qe:{score:.2f}")
