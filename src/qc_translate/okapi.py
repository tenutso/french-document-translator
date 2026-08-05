"""Wrappers around Okapi's Tikal CLI for DOCX <-> XLIFF extraction and merge.

Tikal is a Java tool shipped in the Okapi Applications bundle. bootstrap.sh installs
it to $OKAPI_HOME and puts JAVA on PATH; .env.runtime exports both.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from lxml import etree

from .config import Config

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_LANG_PARTS = re.compile(r"word/(document|styles|header\d*|footer\d*|footnotes|endnotes)\.xml$")


def _tikal(cfg: Config) -> Path:
    okapi = cfg.okapi_home
    tikal = okapi / "tikal.sh"
    if not tikal.exists():
        raise FileNotFoundError(
            f"tikal.sh not found in {okapi}. Run bootstrap.sh (sets OKAPI_HOME)."
        )
    return tikal


def _run(cfg: Config, args: list[str], cwd: Path) -> None:
    env = os.environ.copy()
    # tikal.sh calls a bare `java`; make sure a JDK is on PATH even when the caller's
    # shell never sourced .env.runtime (cfg.java_home falls back to paths.java_home).
    java_home = cfg.java_home
    if java_home:
        env["JAVA_HOME"] = str(java_home)
        env["PATH"] = f"{java_home}/bin:" + env.get("PATH", "")
    elif not shutil.which("java"):
        raise RuntimeError(
            "No Java runtime found: `java` is not on PATH and paths.java_home in "
            f"{cfg.path} does not contain bin/java. Run bootstrap.sh, or "
            f"`source {cfg.path.parent.parent / '.env.runtime'}`."
        )
    cmd = ["bash", str(_tikal(cfg)), *args]
    proc = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Tikal failed ({' '.join(args)}):\n{proc.stdout}\n{proc.stderr}"
        )


def extract(cfg: Config, input_docx: str | Path, work_dir: str | Path) -> Path:
    """Extract a DOCX to a segmented XLIFF. Returns the .xlf path.

    Copies the source into work_dir so Tikal writes the XLIFF (and later reads the
    original for merge) from a stable, per-job location.
    """
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    local_src = work / "source.docx"
    shutil.copy2(input_docx, local_src)

    srx = cfg.repo_path(cfg.language["srx"])
    args = ["-x", local_src.name,
            "-sl", cfg.language["source"], "-tl", cfg.language["target"]]
    if srx.exists():
        args += ["-seg", str(srx)]
    _run(cfg, args, cwd=work)

    xlf = work / f"{local_src.name}.xlf"
    if not xlf.exists():
        raise RuntimeError(f"Expected XLIFF not produced: {xlf}")
    return xlf


def merge(cfg: Config, xliff_path: str | Path,
          out_docx: str | Path, original_docx: str | Path | None = None) -> Path:
    """Merge a translated XLIFF back into the original DOCX format.

    Tikal's `-m` derives the original document from the XLIFF filename (strips `.xlf`),
    so the XLIFF must be named `<original>.docx.xlf` and the original must sit beside it.
    We satisfy that convention regardless of the caller's XLIFF filename.
    """
    xlf = Path(xliff_path)
    work = xlf.parent

    # Locate the original DOCX (extract() leaves it as source.docx in the job dir).
    orig = Path(original_docx) if original_docx else _find_original(work)
    if orig.parent != work:
        shutil.copy2(orig, work / orig.name)
        orig = work / orig.name

    merge_xlf = work / f"{orig.name}.xlf"
    if xlf.resolve() != merge_xlf.resolve():
        shutil.copy2(xlf, merge_xlf)

    _run(cfg, ["-m", merge_xlf.name, "-tl", cfg.language["target"]], cwd=work)

    produced = work / f"{orig.stem}.out{orig.suffix}"
    if not produced.exists():
        candidates = [p for p in work.glob("*.out.docx")]
        if not candidates:
            raise RuntimeError(f"Merge produced no .out.docx in {work}")
        produced = candidates[0]

    out = Path(out_docx)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(produced), str(out))
    finalize_docx(out, target_lang=cfg.language["target"])
    return out


def _serialize(root) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _ensure_docdefaults_lang(styles_root, lang: str) -> None:
    """Set the default run language in styles.xml docDefaults (covers runs with no explicit lang)."""
    def child(parent, tag):
        el = parent.find(f"{{{_W}}}{tag}")
        if el is None:
            el = etree.SubElement(parent, f"{{{_W}}}{tag}")
        return el
    rpr = child(child(child(styles_root, "docDefaults"), "rPrDefault"), "rPr")
    lang_el = rpr.find(f"{{{_W}}}lang")
    if lang_el is None:
        lang_el = etree.SubElement(rpr, f"{{{_W}}}lang")
    lang_el.set(f"{{{_W}}}val", lang)


def _set_run_langs(root, lang: str) -> None:
    """Ensure every run has an explicit <w:lang w:val=lang> in its rPr."""
    for r in root.iter(f"{{{_W}}}r"):
        rpr = r.find(f"{{{_W}}}rPr")
        if rpr is None:
            rpr = etree.Element(f"{{{_W}}}rPr")
            r.insert(0, rpr)          # rPr must be the first child of a run
        lang_el = rpr.find(f"{{{_W}}}lang")
        if lang_el is None:
            lang_el = etree.SubElement(rpr, f"{{{_W}}}lang")
        lang_el.set(f"{{{_W}}}val", lang)


def finalize_docx(docx_path: str | Path, target_lang: str | None = None,
                  update_fields: bool = True) -> None:
    """Post-process a merged DOCX in place: refresh fields and set proofing language.

    - update_fields: <w:updateFields> so Word rebuilds the TOC/page numbers on open.
    - target_lang: set <w:lang w:val="..."> on every run/style/default (and themeFontLang)
      so Word spell-checks in French, not English.
    """
    docx_path = Path(docx_path)
    with zipfile.ZipFile(docx_path) as zin:
        data = {n: zin.read(n) for n in zin.namelist()}

    settings = data.get("word/settings.xml") or f'<w:settings xmlns:w="{_W}"/>'.encode()
    sroot = etree.fromstring(settings)
    if update_fields:
        for el in sroot.findall(f"{{{_W}}}updateFields"):
            sroot.remove(el)
        upd = etree.Element(f"{{{_W}}}updateFields")
        upd.set(f"{{{_W}}}val", "true")
        sroot.insert(0, upd)
    if target_lang:
        tfl = sroot.find(f"{{{_W}}}themeFontLang")
        if tfl is None:
            tfl = etree.SubElement(sroot, f"{{{_W}}}themeFontLang")
        tfl.set(f"{{{_W}}}val", target_lang)
    data["word/settings.xml"] = _serialize(sroot)

    if target_lang:
        for name in list(data):
            if not _LANG_PARTS.match(name):
                continue
            root = etree.fromstring(data[name])
            for lang_el in root.iter(f"{{{_W}}}lang"):   # rewrite existing
                lang_el.set(f"{{{_W}}}val", target_lang)
            if name == "word/styles.xml":
                _ensure_docdefaults_lang(root, target_lang)
            else:
                # Set an EXPLICIT lang on every run (not just docDefaults) so proofing is
                # unambiguous regardless of style inheritance.
                _set_run_langs(root, target_lang)
            data[name] = _serialize(root)

    tmp = docx_path.with_suffix(".tmp.docx")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for n, d in data.items():
            zout.writestr(n, d)
    tmp.replace(docx_path)


def _find_original(work: Path) -> Path:
    """Find the original DOCX in a job dir, ignoring generated outputs."""
    prefer = work / "source.docx"
    if prefer.exists():
        return prefer
    for p in work.glob("*.docx"):
        if not p.name.endswith(".out.docx") and "roundtrip" not in p.name:
            return p
    raise FileNotFoundError(f"No original .docx found next to XLIFF in {work}")
