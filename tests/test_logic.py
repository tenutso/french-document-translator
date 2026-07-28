"""Unit tests for the pure-Python pipeline logic (no Java/vLLM/GPU needed)."""
import os
from pathlib import Path

import pytest

from qc_translate.config import load_config
from qc_translate.models import Segment
from qc_translate.qa import check_segment
from qc_translate.segment_glossary import Glossary
from qc_translate.tm import TranslationMemory, backup_db, plain, restore_db
from qc_translate.xliff import (codes_match, inline_code_ids, mask_inline,
                                unmask_inline, visible_text)

CFG = load_config()


# --- xliff inline-code accounting -------------------------------------------
def test_inline_code_ids_match():
    src = 'Click <g id="1">Save</g> then <x id="2"/> now.'
    tgt = 'Cliquez sur <g id="1">Enregistrer</g> puis <x id="2"/> maintenant.'
    assert inline_code_ids(src) == inline_code_ids(tgt)


def test_inline_code_ids_detect_drop():
    src = 'Click <g id="1">Save</g>.'
    tgt = "Cliquez sur Enregistrer."
    assert inline_code_ids(src) != inline_code_ids(tgt)


# --- inline-code masking -----------------------------------------------------
def test_mask_unmask_roundtrip():
    src = 'Click <g id="1">Save</g> then <x id="2"/> now.'
    masked, codes = mask_inline(src)
    assert masked == "Click ⟦1⟧Save⟦2⟧ then ⟦3⟧ now."  # tags -> placeholders, text natural
    assert len(codes) == 3
    # Simulate a model translation that keeps the placeholders.
    model_out = "Cliquez sur ⟦1⟧Enregistrer⟦2⟧ puis ⟦3⟧ maintenant."
    restored = unmask_inline(model_out, codes)
    assert codes_match(src, restored)
    assert 'g id="1"' in restored and 'x id="2"' in restored


def test_mask_unescapes_entities_for_model():
    src = "Tom &amp; Jerry &lt;note&gt;"
    masked, codes = mask_inline(src)
    assert masked == "Tom & Jerry <note>"  # model sees natural text
    assert unmask_inline("Tom & Jerry <note>", codes) == "Tom &amp; Jerry &lt;note&gt;"


def test_codes_mergeable_allows_reorder_rejects_bad():
    from qc_translate.xliff import codes_mergeable
    src = ('<bpt id="1">a</bpt>One<ept id="1">b</ept> '
           '<bpt id="2">c</bpt>Two<ept id="2">d</ept>')
    # Two bold spans swapped in the translation — same codes, still well-formed.
    reordered = ('<bpt id="2">c</bpt>Deux<ept id="2">d</ept> '
                 '<bpt id="1">a</bpt>Un<ept id="1">b</ept>')
    assert codes_mergeable(src, reordered)
    # Missing a code -> not mergeable.
    assert not codes_mergeable(src, '<bpt id="1">a</bpt>Un<ept id="1">b</ept>')
    # ept before its bpt (bad nesting) -> not mergeable.
    assert not codes_mergeable('<bpt id="1">a</bpt>X<ept id="1">b</ept>',
                               '<ept id="1">b</ept>X<bpt id="1">a</bpt>')


def test_mask_native_codes_hidden_whole():
    # Okapi native codes carry escaped Word markup as content; it must be hidden whole
    # so the model never sees "<run1>" and plaintext isn't polluted.
    src = ('At the <bpt id="1">&lt;run1&gt;</bpt>Business Starter Track'
           '<ept id="1">&lt;/run1&gt;</ept> today.')
    masked, codes = mask_inline(src)
    assert "run1" not in masked                       # native content hidden
    assert masked == "At the ⟦1⟧Business Starter Track⟦2⟧ today."
    model_out = "Au ⟦1⟧Parcours « Démarrage d'entreprise »⟦2⟧ aujourd'hui."
    restored = unmask_inline(model_out, codes)
    assert codes_match(src, restored)
    assert "&lt;run1&gt;" in restored                 # native code restored verbatim


def test_visible_text_ignores_native_code_content():
    # The casing bug: <ph> native content dragged the uppercase ratio below the threshold.
    from qc_translate.translate import _is_allcaps
    src = 'The CAPS ACADEMY<ph id="1">&lt;tags1/&gt;</ph>'
    assert visible_text(src) == "The CAPS ACADEMY"
    assert _is_allcaps(visible_text(src)) is True      # now correctly detected as caps


def test_unmask_tolerates_spacing_and_flags_missing():
    src = 'A <g id="1">b</g> c.'
    _, codes = mask_inline(src)
    # Model added spaces inside placeholders — still restored.
    assert codes_match(src, unmask_inline("A ⟦ 1 ⟧b⟦2⟧ c.", codes))
    # Model dropped a placeholder — codes no longer match (QA will flag).
    assert not codes_match(src, unmask_inline("A b⟦2⟧ c.", codes))


# --- glossary ----------------------------------------------------------------
def test_glossary_match():
    gloss = Glossary.load(CFG.repo_path(CFG.glossary["tbx"]))
    hits = gloss.match("Every Speaker at the Networking event joins the Workshop.")
    assert hits.get("Speaker") == "Conférencier·ère"
    assert hits.get("Networking") == "Réseautage"
    assert hits.get("Workshop") == "Atelier"


def test_glossary_multiword_wins():
    gloss = Glossary.load(CFG.repo_path(CFG.glossary["tbx"]))
    hits = gloss.match("Register for the CAPS Convention today.")
    # Longer term matched; both may be present but the multiword must map correctly.
    assert hits.get("CAPS Convention") == "Convention de CAPS"


# --- QA checks ---------------------------------------------------------------
def test_qa_flags_tag_and_number_and_glossary():
    seg = Segment(unit_id="1",
                  source_xml='Send the <g id="1">email</g> to 3 people.',
                  target_xml="Envoyez le à 5 personnes.")  # dropped tag, wrong number
    seg.glossary_hits = {"email": "courriel"}
    check_segment(seg, CFG)
    assert "tag_mismatch" in seg.qa_flags
    assert "number_mismatch" in seg.qa_flags
    assert any(f.startswith("glossary_miss") for f in seg.qa_flags)


def test_qa_clean_segment():
    seg = Segment(unit_id="2",
                  source_xml='Send the <g id="1">email</g> to 3 people.',
                  target_xml='Envoyez le <g id="1">courriel</g> à 3 personnes.')
    seg.glossary_hits = {"email": "courriel"}
    check_segment(seg, CFG)
    assert seg.qa_flags == []


def test_qa_untranslated():
    seg = Segment(unit_id="3", source_xml="This is a sentence.",
                  target_xml="This is a sentence.")
    check_segment(seg, CFG)
    assert "untranslated" in seg.qa_flags


# --- TM ----------------------------------------------------------------------
def test_tm_exact_and_fuzzy(tmp_path: Path):
    tm = TranslationMemory(tmp_path / "tm.sqlite")
    tm.upsert("Open the file menu.", "Ouvrez le menu Fichier.")
    assert tm.exact("Open the file menu.") == ("Ouvrez le menu Fichier.", "mt")
    assert tm.exact("Something else.") is None
    fuzzy = tm.fuzzy("Open the file menus.", threshold=0.7)
    assert fuzzy and fuzzy[0][1] == "Ouvrez le menu Fichier."
    assert fuzzy[0][3] == "mt"
    tm.close()


def test_plain_strips_tags():
    assert plain('A <g id="1">bold</g> word.') == "A bold word."


def test_tm_key_survives_word_run_resplitting():
    """The revision case: Word re-splits runs on almost any edit.

    Native codes carry their Word markup as *escaped content*, so a key built by merely
    stripping tags kept `&lt;run1&gt;` — and Word renumbers those runs whenever a
    paragraph is touched, so an approved translation stopped matching its own source in
    the next version. Reproduced from NFDBB2FA9-tu59 of the AGM report.
    """
    v1 = ('Facilitating peer-based <bpt id="1">&lt;run1&gt;</bpt>Leader-to-Leader calls'
          '<ept id="1">&lt;/run1&gt;</ept> for members.')
    # Same visible sentence, but Word split "Leader-to-Leader" across two runs and
    # renumbered, so every code id and every escaped run marker differs.
    v2 = ('Facilitating peer-based <bpt id="1">&lt;run3&gt;</bpt>Leader-to-Leader'
          '<ept id="1">&lt;/run3&gt;</ept><bpt id="2">&lt;run4&gt;</bpt> calls'
          '<ept id="2">&lt;/run4&gt;</ept> for members.')
    assert plain(v1) == plain(v2) == "Facilitating peer-based Leader-to-Leader calls for members."
    assert "&lt;" not in plain(v1), "escaped Word markup must not leak into the key"


def test_approved_tm_entry_is_not_clobbered_by_machine_output(tmp_path: Path):
    tm = TranslationMemory(tmp_path / "tm.sqlite")
    tm.upsert("Our members matter.", "Nos membres comptent.", origin="approved")
    tm.upsert("Our members matter.", "Nos membres importent.")          # machine retry
    assert tm.exact("Our members matter.") == ("Nos membres comptent.", "approved")
    # A fresh review still wins.
    tm.upsert("Our members matter.", "Nos membres sont importants.", origin="approved")
    assert tm.exact("Our members matter.")[0] == "Nos membres sont importants."
    tm.close()


def test_fuzzy_ranks_approved_ahead_of_machine(tmp_path: Path):
    tm = TranslationMemory(tmp_path / "tm.sqlite")
    tm.upsert("Open the file menu now.", "Ouvrez le menu Fichier maintenant.")
    tm.upsert("Open the file menu today.", "Ouvrez le menu Fichier aujourd'hui.",
              origin="approved")
    hits = tm.fuzzy("Open the file menu.", threshold=0.7)
    assert hits[0][3] == "approved", "reviewer wording must be offered to the model first"
    tm.close()


def test_tmx_round_trip_preserves_origin(tmp_path: Path):
    """Disaster-recovery path: export, reseed a fresh TM, and origin must survive so an
    approved entry can't be silently downgraded to indistinguishable-from-MT."""
    tm = TranslationMemory(tmp_path / "tm.sqlite")
    tm.upsert("Our members matter.", "Nos membres comptent.", origin="approved")
    tm.upsert("Open the file menu.", "Ouvrez le menu Fichier.")   # mt
    tmx = tm.export_tmx(tmp_path / "export.tmx", "en", "fr-CA")
    tm.close()

    fresh = TranslationMemory(tmp_path / "restored.sqlite")
    n = fresh.import_tmx(tmx, "en", "fr-CA")
    assert n == 2
    assert fresh.exact("Our members matter.") == ("Nos membres comptent.", "approved")
    assert fresh.exact("Open the file menu.") == ("Ouvrez le menu Fichier.", "mt")
    fresh.close()


def test_import_tmx_never_downgrades_existing_approved_entry(tmp_path: Path):
    tm = TranslationMemory(tmp_path / "tm.sqlite")
    tm.upsert("Our members matter.", "Nos membres comptent.")   # mt only, no approval yet
    tmx = tm.export_tmx(tmp_path / "export.tmx", "en", "fr-CA")
    tm.close()

    # A newer pod approved a different wording after the export was taken.
    live = TranslationMemory(tmp_path / "live.sqlite")
    live.upsert("Our members matter.", "Nos membres sont importants.", origin="approved")
    live.import_tmx(tmx, "en", "fr-CA")
    assert live.exact("Our members matter.") == ("Nos membres sont importants.", "approved")
    live.close()


def test_import_tmx_without_origin_prop_uses_default_origin(tmp_path: Path):
    """A TMX from elsewhere (or hand-edited) has no x-origin prop; default_origin governs."""
    tmx = tmp_path / "plain.tmx"
    tmx.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<tmx version="1.4"><header creationtool="other" segtype="sentence" '
        'o-tmf="x" adminlang="en" srclang="en" datatype="plaintext"/>\n<body>\n'
        '  <tu><tuv xml:lang="en"><seg>Hello.</seg></tuv>'
        '<tuv xml:lang="fr-CA"><seg>Bonjour.</seg></tuv></tu>\n'
        '</body></tmx>\n', encoding="utf-8",
    )
    tm = TranslationMemory(tmp_path / "tm.sqlite")
    n = tm.import_tmx(tmx, "en", "fr-CA", default_origin="approved")
    assert n == 1
    assert tm.exact("Hello.") == ("Bonjour.", "approved")
    tm.close()


def test_backup_and_restore_db_preserves_inline_codes(tmp_path: Path):
    """The raw-file backup is the no-caveat path: unlike a TMX round-trip, restoring it
    keeps inline codes, so a formatted segment can still be reused verbatim."""
    coded_src = 'Click <bpt id="1">&lt;b&gt;</bpt>Save<ept id="1">&lt;/b&gt;</ept> now.'
    coded_tgt = 'Cliquez sur <bpt id="1">&lt;b&gt;</bpt>Enregistrer<ept id="1">&lt;/ept&gt;</ept> maintenant.'
    tm = TranslationMemory(tmp_path / "tm.sqlite")
    tm.upsert(coded_src, coded_tgt, origin="approved")
    tm.close()

    backup = backup_db(tmp_path / "tm.sqlite", tmp_path / "backup" / "tm_backup.sqlite")
    assert backup.exists()

    restored = restore_db(backup, tmp_path / "restored" / "tm.sqlite")
    tm2 = TranslationMemory(restored)
    hit = tm2.exact(coded_src)
    assert hit is not None
    assert hit == (coded_tgt, "approved")
    assert '<bpt id="1">' in hit[0], "restore must keep inline codes, unlike a TMX import"
    tm2.close()


def test_restore_db_refuses_to_clobber_nonempty_tm_without_force(tmp_path: Path):
    tm = TranslationMemory(tmp_path / "tm.sqlite")
    tm.upsert("Hello.", "Bonjour.")
    tm.close()
    backup = backup_db(tmp_path / "tm.sqlite", tmp_path / "backup.sqlite")

    live = TranslationMemory(tmp_path / "live.sqlite")
    live.upsert("Something else.", "Autre chose.")
    live.close()

    with pytest.raises(FileExistsError):
        restore_db(backup, tmp_path / "live.sqlite")

    restore_db(backup, tmp_path / "live.sqlite", force=True)
    tm3 = TranslationMemory(tmp_path / "live.sqlite")
    assert tm3.exact("Hello.") == ("Bonjour.", "mt")
    assert tm3.exact("Something else.") is None, "force must overwrite, not merge"
    tm3.close()


def test_legacy_tm_is_rekeyed_and_gets_origin(tmp_path: Path):
    """A pre-migration store is rekeyed in place on first open, once."""
    import sqlite3
    db = tmp_path / "legacy.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE tm (src_plain TEXT PRIMARY KEY, src_xml TEXT NOT NULL, "
                "tgt_xml TEXT NOT NULL, updated TEXT DEFAULT CURRENT_TIMESTAMP)")
    src = '<bpt id="1">&lt;run1&gt;</bpt>Our members<ept id="1">&lt;/run1&gt;</ept> matter.'
    con.execute("INSERT INTO tm VALUES (?,?,?,?)",
                ("&lt;run1&gt;Our members&lt;/run1&gt; matter.", src,
                 "Nos membres comptent.", "2026-01-01"))
    con.commit(); con.close()

    tm = TranslationMemory(db)
    assert tm.exact(src) == ("Nos membres comptent.", "mt")
    keys = [r[0] for r in tm.conn.execute("SELECT src_plain FROM tm")]
    assert keys == ["Our members matter."]
    tm.close()
    # Idempotent: re-opening must not rekey again or lose the row.
    tm2 = TranslationMemory(db)
    assert [r[0] for r in tm2.conn.execute("SELECT src_plain FROM tm")] == ["Our members matter."]
    tm2.close()


# --- revision handling -------------------------------------------------------
def _classify(tmp_path, monkeypatch, sources, seed=()):
    """Run translate_segments with the LLM stubbed out; return the segments."""
    from qc_translate import translate as tr
    tm = TranslationMemory(tmp_path / "tm.sqlite")
    for src, tgt, origin in seed:
        tm.upsert(src, tgt, origin=origin)
    segs = [Segment(unit_id=f"u{i}", source_xml=s) for i, s in enumerate(sources)]
    # Anything reaching the LLM just gets a marker target, so the assertions are about
    # routing and classification, not translation quality.
    monkeypatch.setattr(tr.VLLMClient, "translate",
                        lambda self, segments, system: [setattr(s, "target_xml", "FR")
                                                        for s in segments])
    tr.translate_segments(CFG, segs, tm)
    tm.close()
    return segs


def test_version_status_covers_every_case(tmp_path, monkeypatch):
    approved = "Our members matter."
    machine = "The board met in March."
    changed = "Looking ahead, we will pursue prudent diversification of the portfolio."
    changed_v2 = "Looking ahead, we will pursue prudent diversification of the portfolios."
    segs = _classify(
        tmp_path, monkeypatch,
        [approved, machine, changed_v2, "An entirely unrelated new sentence appears."],
        seed=[(approved, "Nos membres comptent.", "approved"),
              (machine, "Le conseil s'est réuni en mars.", "mt"),
              (changed, "En regardant vers l'avenir…", "mt")],
    )
    assert [s.version_status for s in segs] == [
        "unchanged-approved", "unchanged-mt", "changed", "new"]
    # An approved exact match is reused verbatim — it must never reach the LLM.
    assert segs[0].target_xml == "Nos membres comptent."
    assert segs[2].previous_target == "En regardant vers l'avenir…"


def test_exact_hit_with_incompatible_codes_is_not_reused_verbatim(tmp_path, monkeypatch):
    """The stored target's codes came from another version of the document.

    Reusing it verbatim would trip the merge-safety guard in cli and ship the segment in
    English; it must be demoted to a reference and re-rendered instead.
    """
    src = '<bpt id="1">&lt;run1&gt;</bpt>Our members<ept id="1">&lt;/run1&gt;</ept> matter.'
    stored_plain_fr = "Nos membres comptent."          # no inline codes at all
    segs = _classify(tmp_path, monkeypatch, [src],
                     seed=[(src, stored_plain_fr, "approved")])
    seg = segs[0]
    assert seg.tm_exact is None, "must not be treated as a verbatim reuse"
    assert "tm_codes_incompatible" in seg.qa_flags
    assert seg.target_xml == "FR", "should have been re-translated"
    assert seg.tm_fuzzy and seg.tm_fuzzy[0][1] == stored_plain_fr


def test_write_targets_marks_signed_off_units(tmp_path: Path):
    from qc_translate.xliff import write_targets
    xlf = tmp_path / "in.xlf"
    xlf.write_text(
        '<?xml version="1.0"?><xliff version="1.2" '
        'xmlns="urn:oasis:names:tc:xliff:document:1.2"><file source-language="en" '
        'datatype="x-docx" original="d.docx"><body>'
        '<trans-unit id="1"><source>Alpha</source></trans-unit>'
        '<trans-unit id="2"><source>Beta</source></trans-unit>'
        '</body></file></xliff>', encoding="utf-8")
    out = tmp_path / "out.xlf"
    write_targets(xlf, {"1": "Alpha FR", "2": "Beta FR"}, out,
                  {"1": "signed-off", "2": "needs-review-translation"})
    xml = out.read_text(encoding="utf-8")
    assert 'state="signed-off"' in xml and 'approved="yes"' in xml
    assert 'state="needs-review-translation"' in xml
    # Only the settled unit is locked.
    assert xml.count('approved="yes"') == 1


# --- runpod env loading ------------------------------------------------------
def test_load_injected_secrets_precedence(monkeypatch):
    from qc_translate import runpod_env as re_
    # Shell value wins over dotenv/pid1.
    monkeypatch.setenv("HF_TOKEN", "shell_tok")
    monkeypatch.setattr(re_, "_from_dotenv", lambda: {"HF_TOKEN": "dotenv_tok"})
    monkeypatch.setattr(re_, "_from_pid1", lambda: {"HF_TOKEN": "pid1_tok"})
    re_.load_injected_secrets()
    assert os.environ["HF_TOKEN"] == "shell_tok"


def test_load_injected_secrets_pid1_fallback_and_alias(monkeypatch):
    from qc_translate import runpod_env as re_
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    monkeypatch.setattr(re_, "_from_dotenv", lambda: {})
    monkeypatch.setattr(re_, "_from_pid1", lambda: {"HF_TOKEN": "pid1_tok"})
    re_.load_injected_secrets()
    assert os.environ["HF_TOKEN"] == "pid1_tok"
    assert os.environ["HUGGING_FACE_HUB_TOKEN"] == "pid1_tok"  # alias normalised


# --- degeneracy guard --------------------------------------------------------
def test_degenerate_guard():
    from qc_translate.translate import _degenerate
    # Short source, huge target (the system-prompt-echo failure mode).
    assert _degenerate("Result", "Le texte peut contenir des marqueurs " * 5)
    assert _degenerate("Action", "")            # empty target
    # Legitimate translations are not flagged.
    assert not _degenerate("Result", "Résultat")
    assert not _degenerate("Download the file", "Téléchargez le fichier")
    assert not _degenerate("A" * 200, "B" * 260)  # long source, proportional target


def test_repair_placeholders():
    from qc_translate.translate import _repair_placeholders, _placeholders_present
    codes = ['<bpt id="1">&lt;run1&gt;</bpt>', '<ept id="1">&lt;/run1&gt;</ept>']
    # Model dropped the opening ⟦1⟧; repair prepends it (openings go to the front).
    fixed = _repair_placeholders("Membre professionnel certifié⟦2⟧ – suite", codes)
    assert _placeholders_present(fixed) == {1, 2}
    assert fixed.startswith("⟦1⟧")
    # Dropped a trailing standalone code -> appended.
    fixed2 = _repair_placeholders("Texte ⟦1⟧ ici", ['<x id="1"/>', '<ph id="2">a</ph>'])
    assert _placeholders_present(fixed2) == {1, 2} and fixed2.endswith("⟦2⟧")


def test_repair_keeps_isolated_code_pair_in_order():
    """A dropped <it pos="open"> must not land after its pos="close" mate.

    Regression: `pos` lives in an attribute, not the tag name, so an <it> opening used to
    fall through to the append branch and end up at the very end of the segment — past
    its own close. Okapi then aborts the whole merge with "Unexpected code contents for
    closing code".
    """
    from qc_translate.translate import _repair_placeholders, _placeholders_present
    from qc_translate.xliff import codes_mergeable, unmask_inline

    codes = ['<it id="4" pos="open">&lt;run4&gt;</it>',
             '<it id="4" pos="close">&lt;/run4&gt;</it>',
             '<bpt id="5">&lt;run5&gt;</bpt>', '<ept id="5">&lt;/run5&gt;</ept>']
    source = unmask_inline("⟦1⟧ cette année, avec un total approchant ⟦2⟧600 000 $⟦3⟧.⟦4⟧", codes)

    # Model kept everything but the isolated opening.
    fixed = _repair_placeholders(" cette année, avec un total approchant ⟦2⟧600 000 $⟦3⟧.⟦4⟧", codes)
    assert _placeholders_present(fixed) == {1, 2, 3, 4}
    assert fixed.index("⟦1⟧") < fixed.index("⟦2⟧")
    assert codes_mergeable(source, unmask_inline(fixed, codes))


def test_codes_mergeable_rejects_inverted_isolated_pair():
    """codes_mergeable is the last line of defence before an unmergeable XLIFF ships."""
    from qc_translate.xliff import codes_mergeable, unmask_inline

    codes = ['<it id="4" pos="open">&lt;run4&gt;</it>', '<it id="4" pos="close">&lt;/run4&gt;</it>']
    source = unmask_inline("⟦1⟧texte⟦2⟧", codes)
    assert codes_mergeable(source, unmask_inline("⟦1⟧texte⟦2⟧", codes))
    # Same multiset of codes, but the close now precedes the open -> Okapi would abort.
    assert not codes_mergeable(source, unmask_inline("⟦2⟧texte⟦1⟧", codes))


def test_is_allcaps():
    from qc_translate.translate import _is_allcaps
    assert _is_allcaps("KEYS TO BUILD A SPEAKING BUSINESS")
    assert _is_allcaps("MISSION DE CAPS")
    assert not _is_allcaps("Keys to build a speaking business")
    assert not _is_allcaps("CAPS offers a community")  # mostly lowercase
    assert not _is_allcaps("42")                        # no letters
