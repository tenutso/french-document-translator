"""Translation stage: build prompts, call vLLM (OpenAI-compatible), fill targets.

Exact TM matches are reused verbatim (no LLM call). Otherwise the segment is translated
with the Quebec-FR system prompt, glossary constraints, and any fuzzy TM references.
"""
from __future__ import annotations

import asyncio
import re
from difflib import SequenceMatcher
from pathlib import Path

import httpx

from .config import Config
from .models import CHANGED, NEW, UNCHANGED_APPROVED, UNCHANGED_MT, Segment
from .tm import APPROVED, MT, TranslationMemory, plain
from .xliff import codes_mergeable, mask_inline, unmask_inline, visible_text


_MINIMAL_SYSTEM = ("Translate the text from English into {tgt} (Quebec French). "
                   "Keep any ⟦N⟧ placeholders verbatim. Output only the translation.")

# Flags whose handling leaves seg.target_xml holding the English source, not a translation.
_FALLBACK_FLAGS = ("translation_failed", "degenerate_output", "tag_mismatch")


def _degenerate(src_plain: str, tgt_plain: str) -> bool:
    """Heuristic for a broken translation (e.g. the model echoed the instructions).

    Fires when the target is empty, or when a short source yields a wildly longer target.
    """
    if not tgt_plain:
        return True
    return len(src_plain) <= 40 and len(tgt_plain) > 3 * len(src_plain) + 40


_PH_NUM = re.compile(r"⟦\s*(\d+)\s*⟧")
# An <it> is an *isolated* code whose open/close role lives in pos=, not the tag name.
_OPENING = re.compile(r'<(bpt|g|bx)\b|<it\b[^>]*\bpos\s*=\s*"open"')


def _placeholders_present(text: str) -> set[int]:
    return {int(x) for x in _PH_NUM.findall(text)}


def _anchor_pos(content: str, n: int, *, end: bool) -> int | None:
    """Offset just after (end=True) or just before the ⟦n⟧ placeholder, None if absent."""
    m = re.search(rf"⟦\s*{n}\s*⟧", content)
    if m is None:
        return None
    return m.end() if end else m.start()


def _repair_placeholders(content: str, codes: list[str]) -> str:
    """Re-insert any placeholders the model dropped so the code set matches the source.

    A dropped code is confined to the gap between its surviving neighbours (after ⟦i-1⟧,
    before ⟦i+1⟧) so the restored order still follows the source; within that gap an
    opening goes to the earliest spot and a closing to the latest, so the pair wraps as
    much text as it legitimately can. With no neighbour on that side the gap runs to the
    start/end of the segment, which reproduces the old prepend/append behaviour.

    Order matters beyond tidiness: Okapi refuses to merge a unit whose closing code
    precedes its opening, so appending an opening unconditionally could turn one dropped
    code into a hard merge failure. This keeps the translation (French) mergeable instead
    of reverting the whole segment to English; formatting may wrap slightly differently,
    which QA flags as codes_repaired.
    """
    present = _placeholders_present(content)
    for i, code in enumerate(codes, 1):
        if i in present:
            continue
        if _OPENING.match(code):
            prev = max((j for j in present if j < i), default=None)
            at = None if prev is None else _anchor_pos(content, prev, end=True)
            at = 0 if at is None else at
        else:
            nxt = min((j for j in present if j > i), default=None)
            at = None if nxt is None else _anchor_pos(content, nxt, end=False)
            at = len(content) if at is None else at
        content = content[:at] + f"⟦{i}⟧" + content[at:]
        present.add(i)
    return content


def _is_allcaps(text: str) -> bool:
    """True for uppercase-dominant text (e.g. designed headings/cover titles).

    Tower+ hallucinates on ALL-CAPS input (CONFÉRENCIER -> 'CONFLIENCIER'), so such
    segments are translated in lower case and re-uppercased afterwards.
    """
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 2:
        return False
    return sum(c.isupper() for c in letters) / len(letters) >= 0.8


def build_system_prompt(cfg: Config) -> str:
    style = cfg.repo_path(cfg.glossary["style_guide"])
    guide = style.read_text(encoding="utf-8") if style.exists() else ""
    return (
        "You are a professional localization engine translating technical training "
        f"manuals from {cfg.language['source']} into {cfg.language['target']} "
        "(Quebec French). Translate the user's segment and output ONLY the translation, "
        "with no preamble, quotes, or commentary.\n"
        "The text may contain placeholders like ⟦1⟧, ⟦2⟧ that stand for inline formatting. "
        "Keep every placeholder EXACTLY as written (same digits, same ⟦⟧ characters), in the "
        "positions that wrap the corresponding translated words. Never add, drop, renumber, "
        "translate, or space out placeholders.\n\n"
        f"# Style guide\n{guide}"
    )


def build_user_prompt(seg: Segment, masked_source: str) -> str:
    parts: list[str] = []
    if seg.glossary_hits:
        terms = "\n".join(f'- "{s}" -> "{t}"' for s, t in seg.glossary_hits.items())
        parts.append(
            "Required terminology (use these exact French targets):\n" + terms
        )
    if seg.tm_fuzzy:
        # Separate the two kinds of reference. Presenting unreviewed machine output as
        # "previously approved" (as this once did) teaches the model to reproduce its own
        # earlier mistakes as if they were house style.
        by_origin: dict[str, list[str]] = {}
        for s, t, score, origin in seg.tm_fuzzy[:2]:
            by_origin.setdefault(origin, []).append(
                f'- source: "{plain(s)}"\n  target: "{plain(t)}"  (similarity {score:.0%})'
            )
        for origin, heading in (
            (APPROVED, "Approved translations of similar segments — a reviewer signed these "
                       "off, so match their wording wherever it fits:"),
            (MT, "Previous machine translations of similar segments (unreviewed — treat as a "
                 "hint, not as authority):"),
        ):
            if by_origin.get(origin):
                parts.append(heading + "\n" + "\n".join(by_origin[origin]))
    parts.append(
        "Translate this segment, keeping every ⟦N⟧ placeholder verbatim. "
        "Output only the translated segment:\n" + masked_source
    )
    return "\n\n".join(parts)


class VLLMClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.model = cfg.profile["model"]
        self.llm = cfg.llm
        self.sem = asyncio.Semaphore(cfg.llm.get("max_concurrency", 8))

    async def _request(self, client: httpx.AsyncClient, messages: list[dict],
                       temperature: float) -> str | None:
        payload = {
            "model": self.model, "messages": messages,
            "temperature": temperature, "top_p": self.llm.get("top_p", 0.9),
            "max_tokens": self.llm.get("max_tokens", 1024),
        }
        retries = self.llm.get("max_retries", 4)
        for attempt in range(retries):
            try:
                r = await client.post("/chat/completions", json=payload)
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"].strip()
            except (httpx.HTTPError, KeyError):
                if attempt == retries - 1:
                    return None
                await asyncio.sleep(1.5 * (attempt + 1))
        return None

    async def _one(self, client: httpx.AsyncClient, seg: Segment, system: str) -> None:
        masked_source, codes = mask_inline(seg.source_xml)
        # Case detection uses the human-visible text (codes + their native content removed),
        # so escaped code content like "&lt;tags1/&gt;" can't skew the uppercase ratio.
        visible = visible_text(seg.source_xml)
        src_plain = visible
        temp = self.llm.get("temperature", 0.1)
        # ALL-CAPS input makes Tower+ hallucinate; translate in lower case and restore
        # the uppercase after (placeholders are digits, unaffected by .upper()).
        allcaps = _is_allcaps(visible)
        send_source = masked_source.lower() if allcaps else masked_source

        n = len(codes)
        want = set(range(1, n + 1))

        def casify(content: str) -> str:
            return content.upper() if allcaps else content

        def is_good(content: str) -> bool:
            """Placeholders all present AND not a degenerate (prompt-echo) output."""
            if _placeholders_present(content) != want:
                return False
            return not _degenerate(src_plain, plain(unmask_inline(casify(content), codes)))

        async with self.sem:
            # Primary attempt with the full (brand/style) prompt.
            content = await self._request(client, [
                {"role": "system", "content": system},
                {"role": "user", "content": build_user_prompt(seg, send_source)},
            ], temp)
            if content is None:
                seg.target_xml = seg.source_xml
                seg.qa_flags.append("translation_failed")
                return

            # If placeholders were dropped or the model echoed the prompt, retry once with
            # a minimal prompt (translation models misbehave less without the long system).
            if not is_good(content):
                retry = await self._request(client, [
                    {"role": "system", "content": _MINIMAL_SYSTEM.format(
                        tgt=self.cfg.language["target"])},
                    {"role": "user", "content": send_source},
                ], 0.0)
                if retry is not None and (
                    is_good(retry)
                    # or retry at least preserves all placeholders and the primary didn't
                    or (_placeholders_present(retry) == want
                        and _placeholders_present(content) != want)
                ):
                    content = retry

            cased = casify(content)
            # Degenerate even after retry -> keep source (flagged); merge stays valid.
            if _degenerate(src_plain, plain(unmask_inline(cased, codes))):
                seg.target_xml = seg.source_xml
                seg.qa_flags.append("degenerate_output")
                return
            # Repair any still-missing placeholders so the segment stays French & mergeable.
            if _placeholders_present(cased) != want:
                cased = _repair_placeholders(cased, codes)
                seg.qa_flags.append("codes_repaired")

            target = unmask_inline(cased, codes)
            if not codes_mergeable(seg.source_xml, target):
                seg.target_xml = seg.source_xml   # last resort: valid merge over French-ish
                seg.qa_flags.append("tag_mismatch")
                return
            seg.target_xml = target

    async def _run(self, segments: list[Segment], system: str) -> None:
        base = self.llm["base_url"]
        headers = {"Authorization": f"Bearer {self.llm.get('api_key', 'EMPTY')}"}
        timeout = httpx.Timeout(self.llm.get("request_timeout_s", 180))
        async with httpx.AsyncClient(base_url=base, headers=headers, timeout=timeout) as client:
            await asyncio.gather(*(self._one(client, s, system) for s in segments))

    def translate(self, segments: list[Segment], system: str) -> None:
        asyncio.run(self._run(segments, system))


def translate_text(cfg: Config, text: str) -> str:
    """Translate a bare string (no inline tags). Used for in-image OCR text."""
    if not text.strip():
        return ""
    system = build_system_prompt(cfg)
    payload = {
        "model": cfg.profile["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content":
                "Translate this UI/screenshot text; output only the translation:\n" + text},
        ],
        "temperature": cfg.llm.get("temperature", 0.1),
        "max_tokens": cfg.llm.get("max_tokens", 1024),
    }
    headers = {"Authorization": f"Bearer {cfg.llm.get('api_key', 'EMPTY')}"}
    try:
        r = httpx.post(cfg.llm["base_url"] + "/chat/completions", json=payload,
                       headers=headers, timeout=cfg.llm.get("request_timeout_s", 180))
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except (httpx.HTTPError, KeyError):
        return "(translation unavailable)"


def translate_segments(cfg: Config, segments: list[Segment], tm: TranslationMemory) -> None:
    """Fill seg.target_xml for every segment, using TM first then the LLM."""
    threshold = cfg.tm.get("fuzzy_threshold", 0.8)
    to_translate: list[Segment] = []
    for seg in segments:
        # Segments with no translatable text (only inline codes / whitespace, e.g. an
        # image-only paragraph) are passed through unchanged — sending them to the LLM
        # wastes a call and invites hallucination.
        if not plain(seg.source_xml):
            seg.target_xml = seg.source_xml
            continue
        hit = tm.exact(seg.source_xml)
        if hit is not None:
            target, origin = hit
            # The stored target's inline codes came from whichever document version wrote
            # it, and Word re-splits runs on almost any edit — so they may not fit this
            # source. Reusing it regardless would trip the merge-safety guard in cli and
            # ship the segment in English. Demote it to a top-ranked reference instead and
            # let the LLM re-render the same French with the codes this source needs.
            if codes_mergeable(seg.source_xml, target):
                seg.target_xml = target
                seg.tm_exact = target
                seg.tm_origin = origin
                seg.version_status = (
                    UNCHANGED_APPROVED if origin == APPROVED else UNCHANGED_MT
                )
                continue
            seg.tm_fuzzy = [(seg.source_xml, target, 1.0, origin)]
            seg.previous_target = target
            seg.qa_flags.append("tm_codes_incompatible")
        else:
            seg.tm_fuzzy = tm.fuzzy(seg.source_xml, threshold)
            if seg.tm_fuzzy:
                seg.previous_target = seg.tm_fuzzy[0][1]
        # English that moved (or a segment we must re-render) reads as `changed`; the
        # reviewer needs to look at it either way.
        seg.version_status = CHANGED if seg.tm_fuzzy else NEW
        if seg.tm_fuzzy:
            seg.tm_origin = seg.tm_fuzzy[0][3]
        to_translate.append(seg)

    if to_translate:
        system = build_system_prompt(cfg)
        VLLMClient(cfg).translate(to_translate, system)

    # Update TM with genuine translations only. Two kinds of segment are withheld:
    # those that fell back to their English source, and any target whose inline codes
    # won't merge. Caching either is worse than not caching at all — `tm.exact` would
    # serve it straight back on the next run, skipping the retry/repair path that would
    # otherwise fix it, and an unmergeable hit then gets reverted to English by the
    # merge-safety guard in cli.
    for seg in to_translate:
        if not seg.target_xml or any(f in _FALLBACK_FLAGS for f in seg.qa_flags):
            continue
        if not codes_mergeable(seg.source_xml, seg.target_xml):
            continue
        tm.upsert(seg.source_xml, seg.target_xml)


def annotate_previous_sources(segments: list[Segment], previous_xliff: str | Path) -> int:
    """Fill `previous_source` on changed segments from a previous job's source XLIFF.

    Optional refinement for changes.html: the TM already tells us a segment moved, this
    tells the reviewer *how the English moved*. Matching is on visible text, never on
    trans-unit id — Okapi derives ids from document structure, so adding a single paragraph
    renumbers everything after it and ids are not comparable across versions.
    """
    from .xliff import read_sources
    previous = [p for p in (plain(s) for _, s in read_sources(previous_xliff)) if p]
    filled = 0
    for seg in segments:
        if seg.version_status != CHANGED:
            continue
        q = plain(seg.source_xml)
        best, score = None, 0.0
        for p in previous:
            r = SequenceMatcher(None, q, p).ratio()
            if r > score:
                best, score = p, r
        if best is not None and score >= 0.6:
            seg.previous_source = best
            filled += 1
    return filled
