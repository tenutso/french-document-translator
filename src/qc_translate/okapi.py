"""Wrappers around Okapi's Tikal CLI for DOCX <-> XLIFF extraction and merge.

Tikal is a Java tool shipped in the Okapi Applications bundle. bootstrap.sh installs
it to $OKAPI_HOME and puts JAVA on PATH; .env.runtime exports both.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

from lxml import etree

from .config import Config

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


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
    # tikal.sh calls `java`; make sure JAVA_HOME/bin is reachable.
    if env.get("JAVA_HOME"):
        env["PATH"] = f"{env['JAVA_HOME']}/bin:" + env.get("PATH", "")
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
    enable_update_fields(out)
    return out


def enable_update_fields(docx_path: str | Path) -> None:
    """Set <w:updateFields w:val="true"/> in settings.xml.

    Word TOCs and page-number fields cache their text; this makes Word regenerate all
    fields on open, so the TOC rebuilds from the translated headings (French).
    """
    docx_path = Path(docx_path)
    with zipfile.ZipFile(docx_path) as zin:
        names = zin.namelist()
        settings_name = "word/settings.xml"
        if settings_name in names:
            root = etree.fromstring(zin.read(settings_name))
        else:
            root = etree.fromstring(
                f'<w:settings xmlns:w="{_W}"/>'.encode())
        # Remove any existing updateFields, then insert at the top of <w:settings>.
        for el in root.findall(f"{{{_W}}}updateFields"):
            root.remove(el)
        upd = etree.SubElement(root, f"{{{_W}}}updateFields")
        upd.set(f"{{{_W}}}val", "true")
        root.insert(0, upd)
        new_settings = etree.tostring(root, xml_declaration=True,
                                      encoding="UTF-8", standalone=True)
        data = {n: zin.read(n) for n in names}
    data[settings_name] = new_settings
    # settings.xml must be registered in [Content_Types].xml (it is in any real docx).
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
