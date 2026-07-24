"""Translation memory: SQLite store with exact + fuzzy lookup and TMX export.

Keyed on the *plain text* of the source (inline codes stripped) so reuse survives minor
tag differences. Fuzzy match uses a cheap token-ratio; good enough to surface references
for the LLM and for the human reviewer.
"""
from __future__ import annotations

import re
import sqlite3
from difflib import SequenceMatcher
from pathlib import Path

_TAG = re.compile(r"<[^>]+>")


def plain(xml: str) -> str:
    """Strip inline tags and collapse whitespace for TM keying/comparison."""
    return re.sub(r"\s+", " ", _TAG.sub("", xml)).strip()


class TranslationMemory:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS tm (
                   src_plain TEXT PRIMARY KEY,
                   src_xml   TEXT NOT NULL,
                   tgt_xml   TEXT NOT NULL,
                   updated   TEXT DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        self.conn.commit()
        # In-memory cache of source plaintexts for fuzzy scan.
        self._keys = [r[0] for r in self.conn.execute("SELECT src_plain FROM tm")]

    def exact(self, source_xml: str) -> str | None:
        row = self.conn.execute(
            "SELECT tgt_xml FROM tm WHERE src_plain = ?", (plain(source_xml),)
        ).fetchone()
        return row[0] if row else None

    def fuzzy(self, source_xml: str, threshold: float, limit: int = 3
              ) -> list[tuple[str, str, float]]:
        """Return up to `limit` (src_xml, tgt_xml, score) above threshold, best first."""
        q = plain(source_xml)
        scored: list[tuple[str, float]] = []
        for key in self._keys:
            if key == q:
                continue
            r = SequenceMatcher(None, q, key).ratio()
            if r >= threshold:
                scored.append((key, r))
        scored.sort(key=lambda t: t[1], reverse=True)
        out: list[tuple[str, str, float]] = []
        for key, score in scored[:limit]:
            row = self.conn.execute(
                "SELECT src_xml, tgt_xml FROM tm WHERE src_plain = ?", (key,)
            ).fetchone()
            if row:
                out.append((row[0], row[1], score))
        return out

    def upsert(self, source_xml: str, target_xml: str) -> None:
        key = plain(source_xml)
        if not key:
            return
        self.conn.execute(
            "INSERT INTO tm(src_plain, src_xml, tgt_xml) VALUES(?,?,?) "
            "ON CONFLICT(src_plain) DO UPDATE SET tgt_xml=excluded.tgt_xml, "
            "src_xml=excluded.src_xml, updated=CURRENT_TIMESTAMP",
            (key, source_xml, target_xml),
        )
        self.conn.commit()
        if key not in self._keys:
            self._keys.append(key)

    def export_tmx(self, tmx_path: str | Path, src_lang: str, tgt_lang: str) -> Path:
        from xml.sax.saxutils import escape
        rows = self.conn.execute("SELECT src_xml, tgt_xml FROM tm").fetchall()
        tus = []
        for src, tgt in rows:
            tus.append(
                f'  <tu><tuv xml:lang="{src_lang}"><seg>{escape(plain(src))}</seg></tuv>'
                f'<tuv xml:lang="{tgt_lang}"><seg>{escape(plain(tgt))}</seg></tuv></tu>'
            )
        body = "\n".join(tus)
        tmx = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<tmx version="1.4"><header creationtool="qc-translate" '
            'segtype="sentence" o-tmf="sqlite" adminlang="en" '
            f'srclang="{src_lang}" datatype="plaintext"/>\n<body>\n{body}\n</body></tmx>\n'
        )
        out = Path(tmx_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(tmx, encoding="utf-8")
        return out

    def close(self) -> None:
        self.conn.close()
