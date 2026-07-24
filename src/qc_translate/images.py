"""Embedded-image handling: extract, locate, OCR, suggest FR for in-image text.

Images with text (screenshots, labelled diagrams) can't be translated by editing the
DOCX text stream, so we flag them for graphic recreation and hand the human everything
needed: location in the doc, the English text found, and a suggested French rendering.
"""
from __future__ import annotations

import base64
import io
import zipfile
from pathlib import Path
from typing import Callable, Optional

from lxml import etree

from .config import Config
from .models import ImageInfo

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _rel_map(zf: zipfile.ZipFile) -> dict[str, str]:
    """rId -> media target path (word/media/...)."""
    try:
        rels = etree.fromstring(zf.read("word/_rels/document.xml.rels"))
    except KeyError:
        return {}
    out = {}
    for rel in rels.iterfind(f"{{{REL_NS}}}Relationship"):
        if rel.get("Type", "").endswith("/image"):
            target = rel.get("Target")
            # Targets are relative to word/.
            out[rel.get("Id")] = "word/" + target.lstrip("/").replace("../", "")
    return out


def _para_text(p: etree._Element) -> str:
    return "".join(t.text or "" for t in p.iter(f"{{{W_NS}}}t")).strip()


def _is_heading(p: etree._Element) -> bool:
    ppr = p.find(f"{{{W_NS}}}pPr")
    if ppr is None:
        return False
    style = ppr.find(f"{{{W_NS}}}pStyle")
    return style is not None and str(style.get(f"{{{W_NS}}}val", "")).lower().startswith("heading")


def extract_images(cfg: Config, input_docx: str | Path,
                   translate_fn: Optional[Callable[[str], str]] = None) -> list[ImageInfo]:
    """Walk the document in order, resolving each image's nearest preceding heading."""
    import pytesseract
    from PIL import Image

    pytesseract.pytesseract.tesseract_cmd = cfg.tesseract_bin
    ocr_lang = cfg.images.get("ocr_langs", "eng")
    min_conf = cfg.images.get("min_confidence", 40)

    results: list[ImageInfo] = []
    with zipfile.ZipFile(input_docx) as zf:
        rel_map = _rel_map(zf)
        doc = etree.fromstring(zf.read("word/document.xml"))
        body = doc.find(f"{{{W_NS}}}body")
        current_heading = "(document start)"
        img_index = 0

        for p in body.iter(f"{{{W_NS}}}p"):
            if _is_heading(p):
                current_heading = _para_text(p) or current_heading
            for blip in p.iter(f"{{{A_NS}}}blip"):
                rid = blip.get(f"{{{R_NS}}}embed")
                media = rel_map.get(rid)
                if not media or media not in zf.namelist():
                    continue
                img_index += 1
                info = ImageInfo(
                    media_path=media, rel_id=rid,
                    location=f"under heading “{current_heading}” (image #{img_index})",
                )
                data = zf.read(media)
                try:
                    im = Image.open(io.BytesIO(data)).convert("RGB")
                except Exception:  # noqa: BLE001 - unsupported media (emf/wmf)
                    info.ocr_text = "(unsupported image format for OCR)"
                    results.append(info)
                    continue

                try:
                    info.ocr_text, info.has_text = _ocr(im, ocr_lang, min_conf)
                except Exception as e:  # noqa: BLE001 - OCR must never kill the job
                    info.ocr_text = f"(OCR unavailable: {type(e).__name__})"
                info.thumbnail_data_uri = _thumb(im)
                if info.has_text:
                    info.requires_recreation = True
                    if translate_fn and cfg.images.get("suggest_translation", True):
                        info.suggested_fr = translate_fn(info.ocr_text)
                results.append(info)
    return results


def _ocr(im, lang: str, min_conf: int) -> tuple[str, bool]:
    import pytesseract
    data = pytesseract.image_to_data(im, lang=lang, output_type=pytesseract.Output.DICT)
    words = [
        w for w, c in zip(data["text"], data["conf"])
        if w.strip() and _to_int(c) >= min_conf
    ]
    text = " ".join(words).strip()
    return text, bool(text)


def _to_int(v) -> int:
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return -1


def _thumb(im, max_px: int = 320) -> str:
    from PIL import Image
    im = im.copy()
    im.thumbnail((max_px, max_px), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
