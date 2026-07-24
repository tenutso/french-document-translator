"""Unit tests for the pure-Python pipeline logic (no Java/vLLM/GPU needed)."""
from pathlib import Path

from qc_translate.config import load_config
from qc_translate.models import Segment
from qc_translate.qa import check_segment
from qc_translate.segment_glossary import Glossary
from qc_translate.tm import TranslationMemory, plain
from qc_translate.xliff import (codes_match, inline_code_ids, mask_inline,
                                unmask_inline)

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
    hits = gloss.match("Please check your email and empty the shopping cart.")
    assert hits.get("email") == "courriel"
    assert hits.get("shopping cart") == "panier"


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
    assert tm.exact("Open the file menu.") == "Ouvrez le menu Fichier."
    assert tm.exact("Something else.") is None
    fuzzy = tm.fuzzy("Open the file menus.", threshold=0.7)
    assert fuzzy and fuzzy[0][1] == "Ouvrez le menu Fichier."
    tm.close()


def test_plain_strips_tags():
    assert plain('A <g id="1">bold</g> word.') == "A bold word."


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
