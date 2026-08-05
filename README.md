# qc-translate — EN → Quebec French DOCX translation pipeline

An open-source, non-commercial pipeline that translates large English Word manuals into
**Quebec French (fr-CA)** and reassembles them into an identically-formatted `.docx`, while
respecting a brand guideline / glossary. Built to run on a RunPod PyTorch 2.8 pod.

The pipeline gets the document as close to deliverable as possible and produces a bilingual
XLIFF + QA/image reports so a human reviewer can finish quickly, working worst-segments-first.

## Pipeline

```
input.docx
  → [extract]  Okapi Tikal → bilingual XLIFF (formatting kept as inline codes) + SRX segmentation
  → [glossary] pre-match brand/OQLF terms → per-segment prompt constraints
  → [translate] Tower+ via vLLM; Quebec-FR system prompt; TM reuse
  → [qa]       tag/number/glossary/length checks + CometKiwi QE (+ optional NLLB backtranslation)
  → [review]   emit OmegaT project for the human pass → reviewed XLIFF
  → [merge]    Okapi → translated.docx (identical layout)
  → [images]   OCR embedded images → image_report.html (location, EN text, suggested FR, flag)
Deliverables: translated.docx + bilingual XLIFF + QA report + image report + updated TM
```

## Quick start (fresh RunPod pod)

```bash
git clone <this-repo> /workspace/qc-translate
cd /workspace/qc-translate
bash bootstrap.sh          # installs everything into /workspace (venv, Okapi, JRE, Tesseract, weights)
bash serve.sh start        # launches vLLM with the profile from config/pipeline.yaml
source /workspace/venv/bin/activate
qc-translate run tests/fixtures/sample.docx --out /workspace/jobs/sample
```

`bootstrap.sh` is idempotent and installs into `/workspace` (the persistent network volume), so
re-spinning a pod is fast and reuses cached weights. No Docker.

## Secrets & RunPod environment variables

The pipeline needs `HF_TOKEN` to download **gated** models (CometKiwi QE). Tokens are resolved
in this order (first non-empty wins): **current shell → repo `.env` → container boot env (PID 1)**.

**Recommended for deploys:** set secrets in the **RunPod pod/template → Environment Variables**
(or reference a **RunPod Secret** as `{{ RUNPOD_SECRET_hf }}`). RunPod injects those into the
container's boot process (PID 1). A freshly-spawned shell doesn't always inherit PID 1's env,
so the pipeline back-fills from `/proc/1/environ` automatically:
- Python side: `qc_translate.runpod_env.load_injected_secrets()` runs before every CLI command.
- Bash side: `scripts/runpod_env.sh` is sourced by `bootstrap.sh` and `serve.sh`.

So `qc-translate …`, `bootstrap.sh`, and `serve.sh` all pick up RunPod-template tokens with no
manual `export`. For local/manual use instead, `cp .env.example .env` and fill it in (git-ignored).

Verify a token is visible: `source scripts/runpod_env.sh && echo "${HF_TOKEN:0:4}…"`.

## Web UI (upload / status / download)

A small self-serve UI to upload a source `.docx`, watch status + log, and download the review
package — no command line needed.

```bash
bash webui.sh           # UI on 0.0.0.0:7860 (QC_UI_PORT to change)
```

Access it through **RunPod's built-in HTTP proxy** (expose port 7860 on the pod):

```
https://<POD_ID>-7860.proxy.runpod.net
```

The engine **auto-starts on the first upload** (no need to run `serve.sh` first). Each job runs
as a detached translate → package pipeline and the page shows an engine online/offline badge.
Jobs run with `--skip-qe` (CometKiwi needs the GPU that vLLM holds); run `qc-translate qa <job>`
separately for quality scores when the engine is stopped.

The proxy URL has no login and this handles client documents — treat the URL as sensitive, or
put your own auth in front. (Prefer an SSH tunnel? `ssh -L 7860:localhost:7860 <pod>`.)

## Reviewer workflow & future updates

Treat the pod as a batch engine; the reviewer works off it. The **translation memory (TM)**
is the compounding asset — reviewer corrections and unchanged segments are reused on the next
document, so quality rises and re-work falls over time.

```bash
# 1. Package the reviewer's files (French .docx + reports + instructions) and refresh the TMX
qc-translate package /workspace/jobs/manual
runpodctl send /workspace/jobs/manual/manual_review_package.zip   # pull it off the pod

# 2. Reviewer edits the French .docx in Word (Track Changes) and sends it back.

# 3. Fold their corrections back into the TM (so future docs reuse the approved wording)
qc-translate import-review reviewed.docx --job /workspace/jobs/manual
```

- **`package`** zips `*.fr-CA.draft.docx` + `qa_report.html` + `image_report.html` +
  `translated.xlf` + `REVIEW_INSTRUCTIONS.md`, and refreshes `qc_translate.tmx`.
- **`import-review`** accepts the reviewed **`.docx`** (aligned by exact/fuzzy match to the
  machine French — robust to segmentation drift) or a reviewed **`.xlf`** (id-based, exact —
  the OmegaT/CAT route). Only changed segments update the TM.
- **New source version (v7, v8…):** just `qc-translate run new.docx` — the TM reuses every
  unchanged segment and only translates what changed.
- **Durability without persistent storage:** the TM lives on the `/workspace` volume, so it's
  gone if you don't pay for a persistent one. `qc-translate package` already copies the raw
  database next to the review zip as `qc_translate.sqlite` — pull that off the pod along
  with the zip. On a fresh pod, before your next `qc-translate run`:
  ```bash
  qc-translate restore-tm qc_translate.sqlite
  ```
  This is the **no-caveat** path: it's the exact same file, so every entry (including
  inline formatting codes) is preserved and reuse behaves as if the pod had never gone
  down. It refuses to overwrite an existing non-empty TM unless you pass `--force`. Take a
  standalone snapshot anytime with `qc-translate backup-tm`.

  A `qc_translate.tmx` also travels in every review zip and is refreshed by `export-tm`.
  It's a portable, human-auditable fallback (and the thing you'd hand to another CAT
  tool), but it's **lossy**: TMX only carries plain text, so restoring from it with
  `qc-translate import-tm qc_translate.tmx` reseeds the TM but loses inline codes —
  segments with no formatting reuse verbatim again immediately, while formatted segments
  (bold/italic runs, etc.) fall back to a strong reference for the LLM instead of a blind
  reuse, same as any exact TM hit whose codes don't fit the current document (see
  `translate.py`). Prefer `backup-tm`/`restore-tm`; use `import-tm` only if the raw
  database backup is missing or stale. Neither ever downgrades an existing approved entry.

**OmegaT alternative:** open `omegat_project/` for segment-level review with the glossary + TM;
run `merge` for the final `.docx`, then `import-review reviewed.xlf` to update the TM.

## Configuration

Everything GPU/model/path-specific lives in [`config/pipeline.yaml`](config/pipeline.yaml).
Switch `llm.profile` with one line to match the GPU tier:

| profile | GPU | model |
|---|---|---|
| `l4` | 23GB L4 | Tower+ 9B (FP8) |
| `a6000` | 48GB A6000/A40/L40 | Tower+ 9B (bf16) |
| `a100` | 1× 80GB A100/H100 | Tower+ 9B (bf16, large KV cache) |
| `a100_72b_x2` | 2× 80GB, `tp=2` | Tower+ 72B |

### L4 note (important)
On a 23GB L4, Tower+ 9B in **bf16** leaves only ~0.7GB for KV cache, which starves
concurrent requests and corrupts short segments. The `l4` profile therefore uses **FP8**
(Ada-native), which frees ~8GB of KV cache and enables the full 8192 context with stable
concurrency — with negligible quality loss for translation.

### 72B note (important)
Tower+ 72B does **not** fit on a single 80GB card. The bf16 checkpoint is ~145GB, and even
with vLLM's online FP8 the weights alone (~73GB) exhaust a `0.92 × 80GB` budget — the engine
OOMs in `fp8.py create_weights` before reserving any KV cache. Quantization happens after
load and does not lower that ceiling. Use `a100_72b_x2` on a genuine multi-GPU pod; on one
card, Tower+ 9B is the ceiling.

## Verified

On an L4 (Tower+ 9B, FP8) the fixture manual round-trips end to end: quality Quebec French
with glossary applied, formatting (headings, numbered list, table, footer, image) intact,
clean Okapi merge, and QA/image reports generated. `pytest` covers the pure-Python logic
(masking, glossary, QA, TM, XLIFF I/O, image location, reports).

## Licensing & attribution

Code in this repo: MIT. This pipeline is intended for **non-commercial** use and relies on
**CC-BY-NC-4.0** models; deliverables must credit them:

- **Unbabel Tower / Tower+** — translation model (CC-BY-NC-4.0)
- **Meta NLLB-200** — optional back-translation QA (CC-BY-NC-4.0)
- **Unbabel CometKiwi / xCOMET** — quality estimation (CC-BY-NC-4.0)

Tooling: Okapi Framework (Apache-2.0), vLLM (Apache-2.0), Tesseract (Apache-2.0),
OmegaT (GPL-3.0, used as an external review tool).
