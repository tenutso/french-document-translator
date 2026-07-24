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
bash serve.sh start     # translation engine (vLLM) must be running
bash webui.sh           # UI on :8080 (QC_UI_PORT to change)
```

Open it over an **SSH tunnel** (it has no built-in auth and handles client documents):

```bash
ssh -L 8080:localhost:8080 <pod>     # then browse http://localhost:8080
```

The UI runs each job as a detached pipeline (translate → package) and shows an engine
online/offline badge. It runs with `--skip-qe` (CometKiwi needs the GPU that vLLM holds); run
`qc-translate qa <job>` separately for quality scores when the engine is stopped.

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
- **Durability:** the TM lives on the `/workspace` volume. Keep the exported `qc_translate.tmx`
  (from `package`/`export-tm`) with your client data so the memory survives if the volume is
  ever deleted.

**OmegaT alternative:** open `omegat_project/` for segment-level review with the glossary + TM;
run `merge` for the final `.docx`, then `import-review reviewed.xlf` to update the TM.

## Configuration

Everything GPU/model/path-specific lives in [`config/pipeline.yaml`](config/pipeline.yaml).
Switch `llm.profile` between `l4` (Tower+ 9B) and `a100` (Tower+ 72B) with one line.

### L4 note (important)
On a 23GB L4, Tower+ 9B in **bf16** leaves only ~0.7GB for KV cache, which starves
concurrent requests and corrupts short segments. The `l4` profile therefore uses **FP8**
(Ada-native), which frees ~8GB of KV cache and enables the full 8192 context with stable
concurrency — with negligible quality loss for translation. Prefer a bigger GPU (A100/H100)
for the 72B model when top quality matters.

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
