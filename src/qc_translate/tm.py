"""Translation memory: SQLite store with exact + fuzzy lookup and TMX export.

Keyed on the *human-visible text* of the source (inline codes and the Word markup they
carry removed) so reuse survives the run re-splitting Word does on almost every edit —
that is what lets an approved translation carry across document revisions. Fuzzy match
uses a cheap token-ratio; good enough to surface references for the LLM and the reviewer.

Each entry records its `origin`: 'approved' for wording a human signed off via
import-review, 'mt' for raw machine output. Approved entries outrank machine ones
everywhere — they are never overwritten by MT, and they sort first as prompt references.
"""
from __future__ import annotations

import sqlite3
from difflib import SequenceMatcher
from pathlib import Path

from lxml import etree

from .xliff import visible_text

APPROVED = "approved"
MT = "mt"
_XML_NS = "http://www.w3.org/XML/1998/namespace"

# Bump when the on-disk layout changes; _ensure_schema migrates once, gated on this.
_SCHEMA_VERSION = 1


def plain(xml: str) -> str:
    """Human-visible text of a segment, for TM keying/comparison.

    Delegates to `xliff.visible_text` so TM keys, QA's number check and the reports all
    agree on what counts as text. Merely regex-stripping tags is not enough: native inline
    codes carry their Word markup as *escaped* content, so `<bpt id="1">&lt;run1&gt;</bpt>`
    would leave `&lt;run1&gt;` behind. That pollutes the key — Word renumbers those runs on
    almost any edit, silently losing the match — and feeds a stray digit to `qa._numbers()`.
    """
    return visible_text(xml)


def _outranks(new: tuple[str, str], old: tuple[str, str]) -> bool:
    """True if (origin, updated) `new` should win over `old`. Approved beats MT."""
    if (new[0] == APPROVED) != (old[0] == APPROVED):
        return new[0] == APPROVED
    return (new[1] or "") >= (old[1] or "")


class TranslationMemory:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self._ensure_schema()
        # In-memory cache of source keys for the fuzzy scan.
        self._keys = [r[0] for r in self.conn.execute("SELECT src_plain FROM tm")]

    def _ensure_schema(self) -> None:
        """Create the table, and migrate a legacy store once (gated on user_version).

        Legacy rows were keyed by a `plain()` that left escaped Word markup in the key, so
        every one of those keys is stale under the current function. Rekeying happens here
        rather than behind a separate command so every entry point — CLI, web UI, tests —
        gets a consistent store without anyone having to remember a migration step.
        """
        conn = self.conn
        conn.execute(
            """CREATE TABLE IF NOT EXISTS tm (
                   src_plain TEXT PRIMARY KEY,
                   src_xml   TEXT NOT NULL,
                   tgt_xml   TEXT NOT NULL,
                   origin    TEXT NOT NULL DEFAULT 'mt',
                   updated   TEXT DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        if conn.execute("PRAGMA user_version").fetchone()[0] >= _SCHEMA_VERSION:
            conn.commit()
            return
        if "origin" not in {r[1] for r in conn.execute("PRAGMA table_info(tm)")}:
            conn.execute(f"ALTER TABLE tm ADD COLUMN origin TEXT NOT NULL DEFAULT '{MT}'")
        self._rekey()
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        conn.commit()

    def _rekey(self) -> int:
        """Recompute every key with the current `plain()`. Returns rows dropped.

        Two old keys can collapse onto one new key (that is the point — they differed only
        in Word markup), so collisions are resolved the same way `upsert` would: approved
        wins, then the newer row. A row whose visible text is empty is dropped; `upsert`
        refuses empty keys, so it could never have been matched anyway.
        """
        rows = self.conn.execute(
            "SELECT src_xml, tgt_xml, origin, updated FROM tm"
        ).fetchall()
        best: dict[str, tuple[str, str, str, str]] = {}
        dropped = 0
        for src_xml, tgt_xml, origin, updated in rows:
            key = plain(src_xml)
            if not key:
                dropped += 1
                continue
            cur = best.get(key)
            if cur is None or _outranks((origin, updated), (cur[2], cur[3])):
                best[key] = (src_xml, tgt_xml, origin, updated)
        self.conn.execute("DELETE FROM tm")
        self.conn.executemany(
            "INSERT INTO tm(src_plain, src_xml, tgt_xml, origin, updated) VALUES(?,?,?,?,?)",
            [(k, s, t, o, u) for k, (s, t, o, u) in best.items()],
        )
        return dropped

    def exact(self, source_xml: str) -> tuple[str, str] | None:
        """Return (target_xml, origin) for an exact visible-text match, else None."""
        row = self.conn.execute(
            "SELECT tgt_xml, origin FROM tm WHERE src_plain = ?", (plain(source_xml),)
        ).fetchone()
        return (row[0], row[1]) if row else None

    def fuzzy(self, source_xml: str, threshold: float, limit: int = 3
              ) -> list[tuple[str, str, float, str]]:
        """Return up to `limit` (src_xml, tgt_xml, score, origin) above threshold.

        Approved entries sort ahead of machine output whatever the score, so the prompt
        shows the reviewer's wording first instead of handing the model its own earlier
        guesses as if they were house style.
        """
        q = plain(source_xml)
        out: list[tuple[str, str, float, str]] = []
        for key in self._keys:
            if key == q:
                continue
            score = SequenceMatcher(None, q, key).ratio()
            if score < threshold:
                continue
            row = self.conn.execute(
                "SELECT src_xml, tgt_xml, origin FROM tm WHERE src_plain = ?", (key,)
            ).fetchone()
            if row:
                out.append((row[0], row[1], score, row[2]))
        out.sort(key=lambda t: (t[3] != APPROVED, -t[2]))
        return out[:limit]

    def upsert(self, source_xml: str, target_xml: str, origin: str = MT) -> None:
        """Store a translation. Machine output never overwrites approved wording."""
        key = plain(source_xml)
        if not key:
            return
        # The WHERE on DO UPDATE is evaluated against the conflicting row, so an 'mt'
        # write silently no-ops when a human has already approved this source.
        self.conn.execute(
            "INSERT INTO tm(src_plain, src_xml, tgt_xml, origin) VALUES(?,?,?,?) "
            "ON CONFLICT(src_plain) DO UPDATE SET tgt_xml=excluded.tgt_xml, "
            "src_xml=excluded.src_xml, origin=excluded.origin, updated=CURRENT_TIMESTAMP "
            "WHERE excluded.origin = 'approved' OR tm.origin <> 'approved'",
            (key, source_xml, target_xml, origin),
        )
        self.conn.commit()
        if key not in self._keys:
            self._keys.append(key)

    def export_tmx(self, tmx_path: str | Path, src_lang: str, tgt_lang: str) -> Path:
        """Export every entry as TMX. Each <tu> carries an `x-origin` prop (our own
        extension, ignored by other CAT tools) so `import_tmx` can round-trip the
        approved/mt distinction — without it, a restored TM would treat every entry as
        equally trustworthy and could let raw machine output masquerade as reviewed."""
        from xml.sax.saxutils import escape
        rows = self.conn.execute("SELECT src_xml, tgt_xml, origin FROM tm").fetchall()
        tus = []
        for src, tgt, origin in rows:
            tus.append(
                f'  <tu><prop type="x-origin">{escape(origin)}</prop>'
                f'<tuv xml:lang="{src_lang}"><seg>{escape(plain(src))}</seg></tuv>'
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

    def import_tmx(self, tmx_path: str | Path, src_lang: str, tgt_lang: str,
                    default_origin: str = MT) -> int:
        """Reseed the TM from a TMX export — the disaster-recovery path for a TM whose
        SQLite file was lost (e.g. an ephemeral pod/volume). Matches `<tuv>` by
        `xml:lang` against `src_lang`/`tgt_lang` (falling back to tu order for a TMX from
        elsewhere that doesn't share these codes). Entries are keyed on plain text, same
        as everywhere else in this module — a TMX round-trip never had inline codes to
        begin with, so imported entries reuse verbatim only for segments with no
        formatting; anything else demotes to a fuzzy reference in `translate_segments`,
        which is exactly the existing behaviour for a stale-codes exact hit.

        Only upserts entries that outrank what's already there (approved beats mt), so
        importing on top of a non-empty TM never downgrades existing approved wording.
        Returns the number of entries imported.
        """
        tree = etree.parse(str(tmx_path))
        n = 0
        for tu in tree.iterfind(".//tu"):
            prop = tu.find('prop[@type="x-origin"]')
            origin = prop.text if prop is not None and prop.text in (APPROVED, MT) else default_origin
            tuvs = tu.findall("tuv")
            if len(tuvs) < 2:
                continue
            src_tuv = next((t for t in tuvs if t.get(f"{{{_XML_NS}}}lang") == src_lang), tuvs[0])
            tgt_tuv = next((t for t in tuvs if t.get(f"{{{_XML_NS}}}lang") == tgt_lang), tuvs[1])
            src_seg, tgt_seg = src_tuv.find("seg"), tgt_tuv.find("seg")
            if src_seg is None or tgt_seg is None or not (src_seg.text and tgt_seg.text):
                continue
            self.upsert(src_seg.text, tgt_seg.text, origin=origin)
            n += 1
        return n

    def close(self) -> None:
        self.conn.close()
