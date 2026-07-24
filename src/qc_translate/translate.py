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
from .tm import TranslationMemory

_SENTINEL = "<<<TRANSLATION>>>"


def build_system_prompt(cfg: Config) -> str:
    style = cfg.repo_path(cfg.glossary["style_guide"])
    guide = style.read_text(encoding="utf-8") if style.exists() else ""
    return (
        "You are a professional localization engine translating technical training "
        f"manuals from {cfg.language['source']} into {cfg.language['target']} "
        "(Quebec French). Translate the user's segment and output ONLY the translation, "
        "with no preamble, quotes, or commentary. Preserve all inline XML tags exactly.\n\n"
        f"# Style guide\n{guide}"
    )


def build_user_prompt(seg: Segment) -> str:
    parts: list[str] = []
    if seg.glossary_hits:
        terms = "\n".join(f'- "{s}" -> "{t}"' for s, t in seg.glossary_hits.items())
        parts.append(
            "Required terminology (use these exact French targets):\n" + terms
        )
    if seg.tm_fuzzy:
        refs = "\n".join(
            f'- source: "{s}"\n  target: "{t}"  (similarity {score:.0%})'
            for s, t, score in seg.tm_fuzzy[:2]
        )
        parts.append(
            "Similar previously-approved translations (for consistency, adapt as needed):\n"
            + refs
        )
    parts.append(
        "Translate this segment. Keep inline tags identical and in order. "
        "Output only the translated segment:\n" + seg.source_xml
    )
    return "\n\n".join(parts)


class VLLMClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.model = cfg.profile["model"]
        self.llm = cfg.llm
        self.sem = asyncio.Semaphore(cfg.llm.get("max_concurrency", 8))

    async def _one(self, client: httpx.AsyncClient, seg: Segment, system: str) -> None:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": build_user_prompt(seg)},
            ],
            "temperature": self.llm.get("temperature", 0.1),
            "top_p": self.llm.get("top_p", 0.9),
            "max_tokens": self.llm.get("max_tokens", 1024),
        }
        retries = self.llm.get("max_retries", 4)
        async with self.sem:
            for attempt in range(retries):
                try:
                    r = await client.post("/chat/completions", json=payload)
                    r.raise_for_status()
                    seg.target_xml = r.json()["choices"][0]["message"]["content"].strip()
                    return
                except (httpx.HTTPError, KeyError) as e:
                    if attempt == retries - 1:
                        seg.target_xml = seg.source_xml  # leave source; QA will flag
                        seg.qa_flags.append(f"translation_failed:{type(e).__name__}")
                        return
                    await asyncio.sleep(1.5 * (attempt + 1))

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
