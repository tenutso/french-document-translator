"""qc-translate command-line interface.

Commands:
  run           Full pipeline: docx -> translated draft docx + XLIFF + reports + OmegaT project
  merge         Merge a reviewed XLIFF back into the DOCX
  roundtrip     Extract + merge with NO translation (formatting-fidelity check)
  export-tm     Export the translation memory to TMX
"""
from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from . import okapi, report
from .config import load_config
from .models import Segment
from .segment_glossary import Glossary
from .tm import TranslationMemory

app = typer.Typer(add_completion=False, help="EN -> Quebec French DOCX translation pipeline")
console = Console()


def _segments_from_xliff(xliff: Path) -> list[Segment]:
    from .xliff import read_sources
    return [Segment(unit_id=uid, source_xml=src) for uid, src in read_sources(xliff)]


@app.command()
def run(
    input_docx: Path = typer.Argument(..., exists=True, readable=True),
    out: Path = typer.Option(..., "--out", "-o", help="Output job directory"),
    config: Path = typer.Option(None, "--config", "-c"),
    skip_qe: bool = typer.Option(False, help="Skip CometKiwi quality estimation"),
    skip_images: bool = typer.Option(False, help="Skip embedded-image OCR report"),
):
    """Run the full translation pipeline on a DOCX."""
    from . import qa
    from .translate import translate_segments, translate_text
    from .xliff import write_targets

    cfg = load_config(config)
    out.mkdir(parents=True, exist_ok=True)
    job = input_docx.stem

    console.print(f"[bold]1/7[/] Extracting {input_docx.name} → XLIFF (Okapi)")
    xliff = okapi.extract(cfg, input_docx, out)
    segments = _segments_from_xliff(xliff)
    console.print(f"    {len(segments)} segments")

    console.print("[bold]2/7[/] Glossary pre-match")
    gloss = Glossary.load(cfg.repo_path(cfg.glossary["tbx"]))
    gloss.annotate(segments)

    console.print("[bold]3/7[/] Translating (TM + vLLM)")
    tm = TranslationMemory(cfg.tm["db"])
    translate_segments(cfg, segments, tm)
    tm.export_tmx(cfg.tm["tmx_export"], cfg.language["source"], cfg.language["target"])

    console.print("[bold]4/7[/] Writing target XLIFF")
    from .xliff import codes_match
    target_xliff = out / "translated.xlf"
    # Merge-safety: if a target lost/altered its inline codes, keep the source for that
    # unit so Okapi can always merge. QA still flags it (tag_mismatch) for the reviewer.
    targets = {}
    for s in segments:
        t = s.target_xml or s.source_xml
        targets[s.unit_id] = t if codes_match(s.source_xml, t) else s.source_xml
    write_targets(xliff, targets, target_xliff)

    console.print("[bold]5/7[/] QA checks" + ("" if skip_qe else " + quality estimation"))
    qa.run_checks(cfg, segments)
    if not skip_qe:
        qa.run_quality_estimation(cfg, segments)
    report.write_qa_report(out / "qa_report.html", job, segments,
                           cfg.qe.get("flag_below", 0.75))

    console.print("[bold]6/7[/] Draft DOCX + OmegaT review project")
    okapi.merge(cfg, target_xliff, out / f"{job}.fr-CA.draft.docx")
    from . import omegat_project
    omegat_project.emit(cfg, target_xliff, cfg.tm["tmx_export"], out / "omegat_project")

    if not skip_images:
        console.print("[bold]7/7[/] Image OCR report")
        from .images import extract_images
        images = extract_images(cfg, input_docx, lambda t: translate_text(cfg, t))
        report.write_image_report(out / "image_report.html", job, images)
    else:
        console.print("[bold]7/7[/] Images skipped")

    tm.close()
    flagged = sum(1 for s in segments if s.needs_review)
    console.print(f"\n[green]Done.[/] {flagged}/{len(segments)} segments flagged for review.")
    console.print(f"Artifacts in [bold]{out}[/]: translated.xlf, {job}.fr-CA.draft.docx, "
                  "qa_report.html, image_report.html, omegat_project/")


@app.command()
def merge(
    reviewed_xliff: Path = typer.Argument(..., exists=True),
    out_docx: Path = typer.Option(..., "--out", "-o"),
    original: Path = typer.Option(None, "--original", help="Original DOCX (needed if not "
                                  "beside the XLIFF, e.g. source.docx from the job dir)"),
    config: Path = typer.Option(None, "--config", "-c"),
):
    """Merge a human-reviewed XLIFF back into a final DOCX."""
    cfg = load_config(config)
    result = okapi.merge(cfg, reviewed_xliff, out_docx, original_docx=original)
    console.print(f"[green]Merged →[/] {result}")


@app.command()
def roundtrip(
    input_docx: Path = typer.Argument(..., exists=True),
    out: Path = typer.Option(..., "--out", "-o"),
    config: Path = typer.Option(None, "--config", "-c"),
):
    """Extract + merge with NO translation to verify formatting fidelity."""
    from .xliff import read_sources, write_targets
    cfg = load_config(config)
    out.mkdir(parents=True, exist_ok=True)
    xliff = okapi.extract(cfg, input_docx, out)
    # Copy source into target unchanged.
    targets = {uid: src for uid, src in read_sources(xliff)}
    passthrough = out / "passthrough.xlf"
    write_targets(xliff, targets, passthrough)
    result = okapi.merge(cfg, passthrough, out / f"{input_docx.stem}.roundtrip.docx")
    console.print(f"[green]Round-trip DOCX →[/] {result}")
    console.print("Compare it to the original in Word: layout must be identical.")


@app.command("export-tm")
def export_tm(config: Path = typer.Option(None, "--config", "-c")):
    """Export the translation memory to TMX."""
    cfg = load_config(config)
    tm = TranslationMemory(cfg.tm["db"])
    out = tm.export_tmx(cfg.tm["tmx_export"], cfg.language["source"], cfg.language["target"])
    tm.close()
    console.print(f"[green]TMX →[/] {out}")


if __name__ == "__main__":
    app()
