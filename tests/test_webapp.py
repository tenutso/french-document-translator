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


def test_unknown_job_404(client):
    c, _, _ = client
    assert c.get("/jobs/nope").status_code == 404
    assert c.get("/jobs/nope/download").status_code == 404
