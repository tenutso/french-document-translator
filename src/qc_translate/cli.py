"""qc-translate command-line interface.

Commands:
  run           Full pipeline: docx -> translated draft docx + XLIFF + reports + OmegaT project
  merge         Merge a reviewed XLIFF back into the DOCX
  roundtrip     Extract + merge with NO translation (formatting-fidelity check)
  export-tm     Export the translation memory to TMX
  import-tm     Reseed the translation memory from a TMX (disaster recovery)
"""
from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from . import okapi, report
from .config import load_config
from .models import Segment
from .segment_glossary import Glossary
from .tm import APPROVED, MT, TranslationMemory

app = typer.Typer(add_completion=False, help="EN -> Quebec French DOCX translation pipeline")
console = Console()


@app.callback()
def _bootstrap_env() -> None:
    """Runs before every command: back-fill RunPod-injected secrets (HF_TOKEN, ...)."""
    from .runpod_env import load_injected_secrets
    load_injected_secrets()


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
    previous: Path = typer.Option(None, "--previous", exists=True,
                                  help="Previous job dir for this document; adds the "
                                       "before/after English to changes.html"),
):
    """Run the full translation pipeline on a DOCX.

    Re-running a revised document is the supported way to handle an edited source: the
    translation memory reuses approved French for every segment whose text is unchanged,
    and only new/changed segments reach the LLM. See changes.html in the job directory.
    """
    from . import qa
    from .translate import annotate_previous_sources, translate_segments, translate_text
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
    if previous:
        prev_xlf = previous / "source.docx.xlf"
        if prev_xlf.exists():
            n = annotate_previous_sources(segments, prev_xlf)
            console.print(f"    matched {n} changed segment(s) to {previous.name}")
        else:
            console.print(f"[yellow]--previous:[/] {prev_xlf} not found, skipping diff")

    console.print("[bold]4/7[/] Writing target XLIFF")
    from .models import STATE_BY_STATUS
    from .xliff import codes_mergeable
    target_xliff = out / "translated.xlf"
    # Merge-safety: if a target's inline codes can't merge cleanly, keep the source for
    # that unit so Okapi can always merge. QA still flags it for the reviewer.
    targets = {}
    for s in segments:
        t = s.target_xml or s.source_xml
        targets[s.unit_id] = t if codes_mergeable(s.source_xml, t) else s.source_xml
    # Mark each unit's revision state so a CAT tool locks settled segments on import.
    states = {s.unit_id: STATE_BY_STATUS[s.version_status]
              for s in segments if s.version_status in STATE_BY_STATUS}
    write_targets(xliff, targets, target_xliff, states)

    console.print("[bold]5/7[/] QA checks" + ("" if skip_qe else " + quality estimation"))
    qa.run_checks(cfg, segments)
    if not skip_qe:
        qa.run_quality_estimation(cfg, segments)
    report.write_qa_report(out / "qa_report.html", job, segments,
                           cfg.qe.get("flag_below", 0.75))
    report.write_changes_report(out / "changes.html", job, segments)
    from .models import CHANGED, NEW, UNCHANGED_APPROVED
    reused = sum(1 for s in segments if s.version_status == UNCHANGED_APPROVED)
    fresh = sum(1 for s in segments if s.version_status in (NEW, CHANGED))
    console.print(f"    {reused} approved segment(s) reused verbatim, "
                  f"{fresh} new/changed (see changes.html)")

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

    # Guard against a silent English passthrough: if the engine was down/unreachable, every
    # segment falls back to its English source (flagged translation_failed / degenerate_output).
    # Denominator is the translatable segments only (non-empty source that isn't an exact TM
    # reuse) so TM-only jobs aren't miscounted.
    from .tm import plain
    _FAIL_FLAGS = {"translation_failed", "degenerate_output"}
    translatable = [s for s in segments if plain(s.source_xml) and s.tm_exact is None]
    failed = [s for s in translatable if _FAIL_FLAGS & set(s.qa_flags)]
    if translatable and len(failed) == len(translatable):
        console.print(
            f"\n[bold red]Translation failed:[/] all {len(failed)} translatable segments fell "
            "back to the English source. The translation engine is almost certainly not "
            "running or not reachable — no French was produced.\n"
            "Check it with [bold]bash serve.sh status[/] / [bold]bash serve.sh ensure[/] and "
            "see [bold]/workspace/vllm.log[/]. Not writing a 'done' result for an "
            "English-only document.")
        raise typer.Exit(code=1)
    if failed:
        console.print(
            f"[bold yellow]Warning:[/] {len(failed)}/{len(translatable)} segments could not be "
            "translated and kept their English source (see qa_report.html).")

    flagged = sum(1 for s in segments if s.needs_review)
    console.print(f"\n[green]Done.[/] {flagged}/{len(segments)} segments flagged for review.")
    console.print(f"Artifacts in [bold]{out}[/]: translated.xlf, {job}.fr-CA.draft.docx, "
                  "qa_report.html, changes.html, image_report.html, omegat_project/")


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


@app.command()
def qa(
    job_dir: Path = typer.Argument(..., exists=True, help="Job dir containing translated.xlf"),
    config: Path = typer.Option(None, "--config", "-c"),
    skip_qe: bool = typer.Option(False, help="Skip CometKiwi quality estimation"),
):
    """Re-run QA (and CometKiwi QE) on an already-translated job — no re-translation.

    Useful to add quality scores after a --skip-qe run once an HF token is available.
    Run it when the GPU is free (vLLM stopped), since CometKiwi needs GPU memory.
    """
    from . import qa as qa_mod
    from .xliff import read_sources, read_targets
    cfg = load_config(config)
    xlf = job_dir / "translated.xlf"
    if not xlf.exists():
        raise typer.BadParameter(f"{xlf} not found")

    targets = read_targets(xlf)
    segments = [Segment(unit_id=uid, source_xml=src, target_xml=targets.get(uid))
                for uid, src in read_sources(xlf)]
    Glossary.load(cfg.repo_path(cfg.glossary["tbx"])).annotate(segments)

    qa_mod.run_checks(cfg, segments)
    if not skip_qe:
        console.print(f"Running CometKiwi QE on {len(segments)} segments…")
        qa_mod.run_quality_estimation(cfg, segments)
    out = report.write_qa_report(job_dir / "qa_report.html", job_dir.name, segments,
                                 cfg.qe.get("flag_below", 0.75))
    flagged = sum(1 for s in segments if s.needs_review)
    console.print(f"[green]QA report →[/] {out}  ({flagged}/{len(segments)} flagged)")


@app.command()
def package(
    job_dir: Path = typer.Argument(..., exists=True),
    out: Path = typer.Option(None, "--out", "-o", help="Output .zip (default: <job>_review_package.zip)"),
    config: Path = typer.Option(None, "--config", "-c"),
):
    """Zip the reviewer's files (French .docx + reports + instructions) and refresh the TMX."""
    from . import review
    cfg = load_config(config)
    zip_path, files = review.package(cfg, job_dir, out)
    console.print(f"[green]Review package →[/] {zip_path}")
    for f in files:
        console.print(f"    included: {f.name}")
    console.print("\nPull it off the pod, e.g.:  [bold]runpodctl send " + str(zip_path) + "[/]")


@app.command("import-review")
def import_review_cmd(
    reviewed: Path = typer.Argument(..., exists=True, help="Reviewed .docx or .xlf"),
    job_dir: Path = typer.Option(..., "--job", help="Original job dir (has source.docx.xlf)"),
    config: Path = typer.Option(None, "--config", "-c"),
):
    """Fold a reviewer's corrections back into the translation memory (compounds over time)."""
    from . import review
    cfg = load_config(config)
    updated, total = review.import_review(cfg, reviewed, job_dir)
    console.print(f"[green]TM updated[/] from {updated}/{total} reviewed segments "
                  f"→ {cfg.tm['db']}")


@app.command("export-tm")
def export_tm(config: Path = typer.Option(None, "--config", "-c")):
    """Export the translation memory to TMX."""
    cfg = load_config(config)
    tm = TranslationMemory(cfg.tm["db"])
    out = tm.export_tmx(cfg.tm["tmx_export"], cfg.language["source"], cfg.language["target"])
    tm.close()
    console.print(f"[green]TMX →[/] {out}")


@app.command("import-tm")
def import_tm(
    tmx: Path = typer.Argument(..., exists=True, help="TMX file to reseed the TM from"),
    config: Path = typer.Option(None, "--config", "-c"),
    default_origin: str = typer.Option(
        "mt", "--default-origin",
        help="Origin for entries whose TMX has no x-origin prop (approved|mt); "
             "ignored for our own exports, which already carry it"),
):
    """Reseed the translation memory from a TMX export.

    For a pod without persistent storage: back up the `qc_translate.tmx` this pipeline
    exports (every `run`/`export-tm`/`package` refreshes it) and, on a fresh pod, run this
    before your next `qc-translate run` to restore TM reuse. Existing approved entries are
    never downgraded by an import.
    """
    if default_origin not in (APPROVED, MT):
        raise typer.BadParameter("--default-origin must be 'approved' or 'mt'")
    cfg = load_config(config)
    tm = TranslationMemory(cfg.tm["db"])
    n = tm.import_tmx(tmx, cfg.language["source"], cfg.language["target"],
                       default_origin=default_origin)
    tm.close()
    console.print(f"[green]Imported {n} TM entr{'y' if n == 1 else 'ies'} from[/] {tmx}")


if __name__ == "__main__":
    app()
