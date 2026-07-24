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
