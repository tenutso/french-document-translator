"""Unit tests for the pure-Python pipeline logic (no Java/vLLM/GPU needed)."""
from pathlib import Path

from qc_translate.config import load_config
from qc_translate.models import Segment
from qc_translate.qa import check_segment
from qc_translate.segment_glossary import Glossary
from qc_translate.tm import TranslationMemory, plain
from qc_translate.xliff import inline_code_ids

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
