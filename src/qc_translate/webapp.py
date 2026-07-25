"""Minimal web UI for the translation pipeline.

Upload a source .docx, watch status/log, download the review package. Runs the pipeline as
a detached subprocess (so it survives a web-server restart) and tracks state via small files
in each job dir. FastAPI/uvicorn ship with vLLM, so no extra heavy deps.

Launch: bash webui.sh   (uvicorn on :8080)  — expose only via SSH tunnel / RunPod proxy auth.
"""
from __future__ import annotations

import html
import re
import shutil
import subprocess
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse

from .config import load_config

REPO = Path(__file__).resolve().parent.parent.parent
CFG = load_config()
JOBS = Path(CFG.raw["paths"].get("work_dir", "/workspace/jobs"))
JOBS.mkdir(parents=True, exist_ok=True)
ENV_FILE = REPO / ".env.runtime"

app = FastAPI(title="qc-translate")

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(name: str) -> str:
    return _SAFE.sub("_", Path(name).name) or "input.docx"


def _state(job: Path) -> str:
    f = job / "state"
    return f.read_text().strip() if f.exists() else "unknown"


def _vllm_up() -> bool:
    base = CFG.llm["base_url"].rsplit("/v1", 1)[0]
    try:
        return httpx.get(base + "/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def _package_path(job: Path) -> Path | None:
    zips = list(job.glob("*_review_package.zip"))
    return zips[0] if zips else None


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>qc-translate</title>{refresh}
<style>
 body{{font-family:system-ui,Segoe UI,sans-serif;margin:2rem;max-width:60rem;color:#1a1a1a}}
 h1{{font-size:1.4rem}} a{{color:#2563eb}}
 .card{{border:1px solid #e4e4e7;border-radius:8px;padding:1rem;margin:1rem 0}}
 table{{border-collapse:collapse;width:100%}} td,th{{border:1px solid #e4e4e7;padding:.5rem;text-align:left;font-size:.9rem}}
 .badge{{padding:.15rem .5rem;border-radius:999px;font-size:.8rem;font-weight:600}}
 .running{{background:#fef9c3}} .done{{background:#dcfce7}} .error{{background:#fee2e2}} .unknown{{background:#f4f4f5}}
 .up{{color:#16a34a}} .down{{color:#b91c1c}}
 pre{{background:#0b1021;color:#e2e8f0;padding:1rem;border-radius:8px;overflow:auto;max-height:60vh}}
 input[type=file]{{margin:.5rem 0}}
 button{{background:#2563eb;color:#fff;border:0;border-radius:6px;padding:.5rem 1rem;cursor:pointer}}
</style></head><body>{body}</body></html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    up = _vllm_up()
    health = (f'<p>Translation engine: <span class="{"up" if up else "down"}">'
              f'{"● online" if up else "● offline — starts automatically on upload"}</span></p>')
    rows = ""
    for job in sorted(JOBS.glob("*/"), reverse=True):
        if not (job / "state").exists() and not (job / "run.log").exists():
            continue
        st = _state(job)
        name = (job / "filename").read_text().strip() if (job / "filename").exists() else job.name
        pkg = _package_path(job)
        dl = f'<a href="/jobs/{job.name}/download">download</a>' if pkg else "—"
        rows += (f'<tr><td>{html.escape(name)}</td>'
                 f'<td><span class="badge {st}">{st}</span></td>'
                 f'<td><a href="/jobs/{job.name}">status/log</a></td>'
                 f'<td>{dl}</td></tr>')
    table = (f'<table><tr><th>Document</th><th>Status</th><th>Details</th><th>Package</th></tr>'
             f'{rows}</table>') if rows else "<p>No jobs yet.</p>"
    body = f"""<h1>EN → Quebec French translation</h1>{health}
<div class="card"><form action="/jobs" method="post" enctype="multipart/form-data">
  <label>Upload a source Word document (.docx):</label><br>
  <input type="file" name="file" accept=".docx" required><br>
  <button type="submit">Translate</button>
</form></div>
<div class="card"><h2 style="font-size:1.1rem">Jobs</h2>{table}</div>"""
    return PAGE.format(refresh='<meta http-equiv="refresh" content="10">', body=body)


@app.post("/jobs")
async def create_job(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".docx"):
        raise HTTPException(400, "Please upload a .docx file")
    if not ENV_FILE.exists():
        raise HTTPException(500, "Run bootstrap.sh first (.env.runtime missing)")
    jid = time.strftime("%Y%m%d-%H%M%S")
    jd = JOBS / jid
    jd.mkdir(parents=True, exist_ok=True)
    upload = jd / ("input_" + _safe_name(file.filename))
    with open(upload, "wb") as f:
        shutil.copyfileobj(file.file, f)
    (jd / "filename").write_text(file.filename)
    (jd / "state").write_text("running")

    # Detached runner: auto-start the engine -> translate (skip GPU-contending QE)
    # -> package -> record state. If the engine never becomes healthy, `serve.sh ensure`
    # exits non-zero; gate the translate step on it so a dead engine fails the job loudly
    # instead of producing an English-only passthrough.
    cmd = (
        f'source "{ENV_FILE}"; cd "{REPO}"; '
        f'if bash "{REPO}/serve.sh" ensure >> "{jd}/run.log" 2>&1; then '
        f'  qc-translate run "{upload}" --out "{jd}" --skip-qe >> "{jd}/run.log" 2>&1; rc=$?; '
        f'  if [ $rc -eq 0 ]; then qc-translate package "{jd}" >> "{jd}/run.log" 2>&1; fi; '
        f'else '
        f'  echo "ERROR: translation engine did not become healthy — see /workspace/vllm.log" '
        f'    >> "{jd}/run.log" 2>&1; rc=1; '
        f'fi; '
        f'echo $rc > "{jd}/returncode"; '
        f'[ $rc -eq 0 ] && echo done > "{jd}/state" || echo error > "{jd}/state"'
    )
    subprocess.Popen(["setsid", "bash", "-c", cmd],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return RedirectResponse(f"/jobs/{jid}", status_code=303)


@app.get("/jobs/{jid}", response_class=HTMLResponse)
def job_status(jid: str) -> str:
    jd = JOBS / _safe_name(jid)
    if not jd.exists():
        raise HTTPException(404, "job not found")
    st = _state(jd)
    log = (jd / "run.log").read_text(errors="replace")[-8000:] if (jd / "run.log").exists() else ""
    name = (jd / "filename").read_text().strip() if (jd / "filename").exists() else jd.name
    pkg = _package_path(jd)
    dl = (f'<p><a href="/jobs/{jd.name}/download"><button>Download review package</button></a></p>'
          if pkg else "")
    refresh = '<meta http-equiv="refresh" content="5">' if st == "running" else ""
    body = (f'<p><a href="/">← all jobs</a></p><h1>{html.escape(name)}</h1>'
            f'<p>Status: <span class="badge {st}">{st}</span></p>{dl}'
            f'<h2 style="font-size:1.1rem">Log</h2><pre>{html.escape(log) or "(waiting…)"}</pre>')
    return PAGE.format(refresh=refresh, body=body)


@app.get("/jobs/{jid}/log", response_class=PlainTextResponse)
def job_log(jid: str) -> str:
    f = JOBS / _safe_name(jid) / "run.log"
    if not f.exists():
        raise HTTPException(404, "no log yet")
    return f.read_text(errors="replace")


@app.get("/jobs/{jid}/download")
def job_download(jid: str):
    pkg = _package_path(JOBS / _safe_name(jid))
    if not pkg:
        raise HTTPException(404, "package not ready")
    return FileResponse(str(pkg), filename=pkg.name, media_type="application/zip")


@app.get("/health", response_class=PlainTextResponse)
def health() -> str:
    return "ok"
