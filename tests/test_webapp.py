"""Tests for the web UI (no pipeline actually launched — Popen is stubbed)."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qc_translate import webapp


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "JOBS", tmp_path)
    monkeypatch.setattr(webapp, "_vllm_up", lambda: True)
    monkeypatch.setattr(webapp, "ENV_FILE", tmp_path / ".env.runtime")
    (tmp_path / ".env.runtime").write_text("")
    launched = {}
    monkeypatch.setattr(webapp.subprocess, "Popen",
                        lambda *a, **k: launched.setdefault("cmd", a))
    return TestClient(webapp.app), tmp_path, launched


def test_index_ok(client):
    c, _, _ = client
    r = c.get("/")
    assert r.status_code == 200 and "translation" in r.text.lower()


def test_health(client):
    c, _, _ = client
    assert c.get("/health").text == "ok"


def test_reject_non_docx(client):
    c, _, _ = client
    r = c.post("/jobs", files={"file": ("notes.txt", b"hi", "text/plain")})
    assert r.status_code == 400


def test_upload_creates_job_and_launches(client):
    c, jobs, launched = client
    r = c.post("/jobs", files={"file": ("Manual.docx", b"PK\x03\x04fake", "application/octet-stream")},
               follow_redirects=False)
    assert r.status_code == 303
    job_dirs = list(jobs.glob("*/"))
    assert len(job_dirs) == 1
    jd = job_dirs[0]
    assert (jd / "state").read_text() == "running"
    assert (jd / "filename").read_text() == "Manual.docx"
    assert list(jd.glob("input_*.docx"))          # sanitized upload saved
    assert "cmd" in launched                        # pipeline was launched
    runner = launched["cmd"][0][3]                  # ["setsid","bash","-c", <cmd>]
    assert "serve.sh" in runner and "ensure" in runner   # engine auto-start
    assert "qc-translate run" in runner


def test_unknown_job_404(client):
    c, _, _ = client
    assert c.get("/jobs/nope").status_code == 404
    assert c.get("/jobs/nope/download").status_code == 404


def _done_job(jobs: Path, jid: str = "job1") -> Path:
    """A completed job dir: has translated.xlf (what import-review aligns against) + state=done."""
    jd = jobs / jid
    jd.mkdir()
    (jd / "translated.xlf").write_text("<xliff/>")
    (jd / "state").write_text("done")
    (jd / "filename").write_text("Manual.docx")
    return jd


def test_submit_review_launches(client):
    c, jobs, launched = client
    jd = _done_job(jobs)
    r = c.post(f"/jobs/{jd.name}/review",
               files={"file": ("reviewed.xlf", b"<xliff/>", "application/xml")},
               follow_redirects=False)
    assert r.status_code == 303
    assert (jd / "review_state").read_text() == "running"
    assert list(jd.glob("reviewed_*.xlf"))                 # sanitized reviewed file saved
    runner = launched["cmd"][0][3]                          # ["setsid","bash","-c", <cmd>]
    assert "import-review" in runner and "--job" in runner
    assert "qc-translate merge" in runner                   # .xlf path re-merges to final docx


def test_submit_review_docx_no_merge(client):
    c, jobs, launched = client
    jd = _done_job(jobs, "job2")
    r = c.post(f"/jobs/{jd.name}/review",
               files={"file": ("reviewed.docx", b"PK\x03\x04", "application/octet-stream")},
               follow_redirects=False)
    assert r.status_code == 303
    runner = launched["cmd"][0][3]
    assert "import-review" in runner
    assert 'cp "' in runner                                 # reviewed .docx is copied as final


def test_submit_review_rejects_bad_ext(client):
    c, jobs, _ = client
    _done_job(jobs, "job3")
    r = c.post("/jobs/job3/review",
               files={"file": ("notes.txt", b"hi", "text/plain")})
    assert r.status_code == 400


def test_submit_review_requires_completed_job(client):
    c, jobs, _ = client
    (jobs / "job4").mkdir()                                 # exists but no translated.xlf
    r = c.post("/jobs/job4/review",
               files={"file": ("reviewed.xlf", b"<xliff/>", "application/xml")})
    assert r.status_code == 400


def test_final_and_tmx_404_when_absent(client):
    c, jobs, _ = client
    jd = _done_job(jobs, "job5")
    assert c.get(f"/jobs/{jd.name}/final").status_code == 404
    assert c.get(f"/jobs/{jd.name}/tmx").status_code == 404   # no review submitted yet
