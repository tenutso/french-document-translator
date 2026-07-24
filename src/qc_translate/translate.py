"""Translation stage: build prompts, call vLLM (OpenAI-compatible), fill targets.

Exact TM matches are reused verbatim (no LLM call). Otherwise the segment is translated
with the Quebec-FR system prompt, glossary constraints, and any fuzzy TM references.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from .config import Config
from .models import Segment
from .tm import TranslationMemory, plain
from .xliff import mask_inline, unmask_inline


_MINIMAL_SYSTEM = ("Translate the text from English into {tgt} (Quebec French). "
                   "Keep any ⟦N⟧ placeholders verbatim. Output only the translation.")


def _degenerate(src_plain: str, tgt_plain: str) -> bool:
    """Heuristic for a broken translation (e.g. the model echoed the instructions).

    Fires when the target is empty, or when a short source yields a wildly longer target.
    """
    if not tgt_plain:
        return True
    return len(src_plain) <= 40 and len(tgt_plain) > 3 * len(src_plain) + 40


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
        refs = "\n".join(
            f'- source: "{plain(s)}"\n  target: "{plain(t)}"  (similarity {score:.0%})'
            for s, t, score in seg.tm_fuzzy[:2]
        )
        parts.append(
            "Similar previously-approved translations (for consistency, adapt as needed):\n"
            + refs
        )
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
        src_plain = plain(seg.source_xml)
        temp = self.llm.get("temperature", 0.1)
        # ALL-CAPS input makes Tower+ hallucinate; translate in lower case and restore
        # the uppercase after (placeholders are digits, unaffected by .upper()).
        allcaps = _is_allcaps(src_plain)
        send_source = masked_source.lower() if allcaps else masked_source

        def finalize(content: str) -> str:
            return unmask_inline(content.upper() if allcaps else content, codes)

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
            target = finalize(content)

            # Guard: translation-specialized models sometimes echo/translate the long
            # system prompt on short, ambiguous segments. Detect that and retry with a
            # minimal prompt; if it still misbehaves, keep the source (flagged).
            if _degenerate(src_plain, plain(target)):
                content = await self._request(client, [
                    {"role": "system", "content": _MINIMAL_SYSTEM.format(
                        tgt=self.cfg.language["target"])},
                    {"role": "user", "content": send_source},
                ], 0.0)
                target = finalize(content) if content else seg.source_xml
                if content is None or _degenerate(src_plain, plain(target)):
                    seg.target_xml = seg.source_xml
                    seg.qa_flags.append("degenerate_output")
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
        exact = tm.exact(seg.source_xml)
        if exact is not None:
            seg.target_xml = exact
            seg.tm_exact = exact
            continue
        seg.tm_fuzzy = tm.fuzzy(seg.source_xml, threshold)
        to_translate.append(seg)

    if to_translate:
        system = build_system_prompt(cfg)
        VLLMClient(cfg).translate(to_translate, system)

    # Update TM with fresh translations (skip failures).
    for seg in to_translate:
        if seg.target_xml and "translation_failed" not in " ".join(seg.qa_flags):
            tm.upsert(seg.source_xml, seg.target_xml)
