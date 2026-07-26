"""Read/write XLIFF 1.2 produced by Okapi Tikal.

We treat each <trans-unit> as a segment whose translatable content is the *inner XML*
of <source>. Inline formatting codes (<g>, <x/>, <bpt/>, <ept/>, <ph/>, <it/>) are kept
verbatim; the translator must reproduce them. Writing targets injects a <target> with the
same inline codes.
"""
from __future__ import annotations

import re
from pathlib import Path

from lxml import etree

XLIFF_NS = "urn:oasis:names:tc:xliff:document:1.2"
_NS = {"x": XLIFF_NS}
# Inline code elements defined by XLIFF 1.2.
_INLINE_TAGS = ("g", "x", "bx", "ex", "bpt", "ept", "ph", "it", "sub", "mrk")


_XLIFF_NS_DECL = re.compile(r'\s+xmlns="urn:oasis:names:tc:xliff:document:1\.2"')


def _inner_xml(el: etree._Element) -> str:
    """Serialize the children of an element (text + inline tags) as a string.

    lxml re-declares the default XLIFF namespace on each serialized child; strip it so
    inline codes read cleanly (e.g. `<g id="1">`). They re-inherit the namespace when
    wrapped back into a <target> in `_build_target`.
    """
    parts = []
    if el.text:
        parts.append(_escape_text(el.text))
    for child in el:
        parts.append(etree.tostring(child, encoding="unicode"))
    return _XLIFF_NS_DECL.sub("", "".join(parts))


def _escape_text(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_ANY_TAG = re.compile(r"<[^>]+>")
_PLACEHOLDER = re.compile(r"⟦\s*(\d+)\s*⟧")
# Okapi "native" inline codes carry the original Word markup as their (escaped) content,
# e.g. <ph id="1">&lt;tags1/&gt;</ph> or <bpt id="1">&lt;run1&gt;</bpt>. That content is
# NOT translatable and must be hidden whole — leaking it (as <tags1/>, <run1>) confuses
# the model (dropped placeholders -> reverts) and pollutes plaintext (breaks case detection).
# <g>/<mrk> are excluded: their content IS translatable, so only their tags are masked.
_NATIVE_PAIRED = re.compile(r"<(bpt|ept|ph|it|sub)\b[^>]*>.*?</\1\s*>", re.DOTALL)


def _unescape_text(t: str) -> str:
    return t.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def visible_text(source_xml: str) -> str:
    """Human-visible text of a segment: inline codes (and their native content) removed."""
    masked, _ = mask_inline(source_xml)
    return re.sub(r"\s+", " ", _PLACEHOLDER.sub("", masked)).strip()


def mask_inline(source_xml: str) -> tuple[str, list[str]]:
    """Replace inline codes with opaque ⟦N⟧ placeholders for the LLM.

    Returns (masked_text, codes) where masked_text is natural (un-escaped) text with
    numbered placeholders, and codes[i] is the original code string for ⟦i+1⟧. Native
    paired codes are masked whole (tag + escaped content + close) so the model never sees
    the raw Word markup; remaining standalone tags (<g>, </g>, <x/>, <mrk> ...) are masked
    individually, keeping any translatable text between them.
    """
    codes: list[str] = []

    def repl(m: re.Match) -> str:
        codes.append(m.group(0))
        return f"⟦{len(codes)}⟧"

    masked = _NATIVE_PAIRED.sub(repl, source_xml)  # whole native codes first
    masked = _ANY_TAG.sub(repl, masked)            # then any remaining tags
    return _unescape_text(masked), codes


def unmask_inline(text: str, codes: list[str]) -> str:
    """Rebuild inner XLIFF XML: escape the translated text, restore original codes.

    Placeholders (⟦N⟧) survive XML-escaping untouched, so we escape first then swap them
    back for the exact original tag strings. Tolerates minor spacing (⟦ 1 ⟧).
    """
    escaped = _escape_text(text)

    def repl(m: re.Match) -> str:
        idx = int(m.group(1))
        return codes[idx - 1] if 1 <= idx <= len(codes) else ""

    return _PLACEHOLDER.sub(repl, escaped)


def codes_match(a_xml: str, b_xml: str) -> bool:
    """True if two inner-XML strings carry the same inline codes in the same order."""
    return inline_code_ids(a_xml) == inline_code_ids(b_xml)


def codes_mergeable(src_xml: str, tgt_xml: str) -> bool:
    """True if the target's inline codes will merge cleanly, allowing reordering.

    Okapi matches codes by id, so a valid translation may reorder inline spans (e.g. two
    bold phrases swap). We require: (1) the same multiset of codes as the source, and
    (2) well-formed pairing — each opening code precedes its closing mate (bpt#k before
    ept#k, it#k/open before it#k/close). This is stricter than merge actually needs but
    safe, and far less trigger-happy than exact-order codes_match.
    """
    from collections import Counter
    src_ids = inline_code_ids(src_xml)
    tgt_ids = inline_code_ids(tgt_xml)
    if Counter(src_ids) != Counter(tgt_ids):
        return False
    first: dict[str, int] = {}
    for i, tok in enumerate(tgt_ids):
        first.setdefault(tok, i)
    for tok in tgt_ids:
        mate = _closing_mate(tok)
        if mate is not None and mate in first and first[tok] > first[mate]:
            return False
    return True


def _closing_mate(tok: str) -> str | None:
    """The identity token that must follow `tok`, or None if `tok` isn't an opening code."""
    if tok.startswith("bpt#"):
        return "ept#" + tok.split("#", 1)[1]
    if tok.endswith("/open"):
        return tok[: -len("open")] + "close"
    return None


def inline_code_ids(xml: str) -> list[str]:
    """Ordered list of inline-code identity tokens in a source/target string.

    Used by QA to assert the target preserves exactly the same inline codes as the
    source. We key on tag name + id attribute so order and multiplicity are checked,
    plus `pos` for <it>: an isolated code carries its open/close role in that attribute
    rather than in the tag name, so without it an inverted pair (close before open —
    which Okapi refuses to merge) is indistinguishable from a correct one.
    """
    ids: list[str] = []
    for m in re.finditer(r"<\s*(\w+)([^>]*?)/?>", xml):
        tag = m.group(1)
        if tag in _INLINE_TAGS:
            attrs = m.group(2)
            id_m = re.search(r'\bid\s*=\s*"([^"]*)"', attrs)
            pos_m = re.search(r'\bpos\s*=\s*"([^"]*)"', attrs)
            tok = f"{tag}#{id_m.group(1) if id_m else ''}"
            ids.append(f"{tok}/{pos_m.group(1)}" if pos_m else tok)
    return ids


def read_sources(path: str | Path) -> list[tuple[str, str]]:
    """Return [(trans-unit id, source inner-xml)] for translatable units."""
    tree = etree.parse(str(path))
    out: list[tuple[str, str]] = []
    for tu in tree.iterfind(f".//{{{XLIFF_NS}}}trans-unit"):
        if tu.get("translate") == "no":
            continue
        src = tu.find(f"{{{XLIFF_NS}}}source")
        if src is None:
            continue
        out.append((tu.get("id"), _inner_xml(src)))
    return out


def read_targets(path: str | Path) -> dict[str, str]:
    """Return {trans-unit id: target inner-xml} for units that have a <target>."""
    tree = etree.parse(str(path))
    out: dict[str, str] = {}
    for tu in tree.iterfind(f".//{{{XLIFF_NS}}}trans-unit"):
        tgt = tu.find(f"{{{XLIFF_NS}}}target")
        if tgt is not None:
            out[tu.get("id")] = _inner_xml(tgt)
    return out


def write_targets(path: str | Path, targets: dict[str, str], out_path: str | Path) -> None:
    """Write a <target> (with inner XML) into each trans-unit and save to out_path."""
    parser = etree.XMLParser(remove_blank_text=False)
    tree = etree.parse(str(path), parser)
    root = tree.getroot()
    for tu in root.iterfind(f".//{{{XLIFF_NS}}}trans-unit"):
        uid = tu.get("id")
        if uid not in targets:
            continue
        # Remove any existing target, then build a new one from the string payload.
        for existing in tu.findall(f"{{{XLIFF_NS}}}target"):
            tu.remove(existing)
        target_el = _build_target(targets[uid])
        # Insert target right after source for a valid, tidy XLIFF.
        src = tu.find(f"{{{XLIFF_NS}}}source")
        src.addnext(target_el)
    tree.write(str(out_path), encoding="UTF-8", xml_declaration=True)


def _build_target(inner_xml: str) -> etree._Element:
    """Parse an inner-XML target string into a <target> element in the XLIFF ns."""
    wrapped = f'<target xmlns="{XLIFF_NS}">{inner_xml}</target>'
    try:
        return etree.fromstring(wrapped)
    except etree.XMLSyntaxError:
        # Model returned text that broke tag well-formedness; fall back to text-only
        # target so the merge still succeeds (QA will have flagged the tag mismatch).
        el = etree.Element(f"{{{XLIFF_NS}}}target")
        el.text = re.sub(r"<[^>]+>", "", inner_xml)
        return el
