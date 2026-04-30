import json
import time
from pathlib import Path

import pytest

from codex_async_mcp import job_manager
from codex_async_mcp.config import JOBS_DIR


def test_start_job_creates_files(tmp_path, monkeypatch):
    monkeypatch.setattr(job_manager, "JOBS_DIR", tmp_path)

    import codex_async_mcp.job_manager as jm
    monkeypatch.setattr(jm, "JOBS_DIR", tmp_path)

    # Use 'echo' as a stand-in for codex so it exits quickly
    monkeypatch.setattr(jm, "CODEX_BIN", "echo")
    import codex_async_mcp.config as cfg
    monkeypatch.setattr(cfg, "CODEX_BIN", "echo")
    monkeypatch.setattr(cfg, "JOBS_DIR", tmp_path)

    result = jm.start_job("hello world", str(tmp_path))

    job_id = result["job_id"]
    assert result["status"] == "running"
    assert len(job_id) == 8

    job_dir = tmp_path / job_id
    assert (job_dir / "meta.json").exists()
    assert (job_dir / "output.txt").exists()

    meta = json.loads((job_dir / "meta.json").read_text())
    assert meta["job_id"] == job_id
    assert meta["prompt"] == "hello world"
    assert meta["status"] == "running"


def test_poll_unknown_job(tmp_path, monkeypatch):
    import codex_async_mcp.job_manager as jm
    monkeypatch.setattr(jm, "JOBS_DIR", tmp_path)

    result = jm.poll_job("nonexistent")
    assert "error" in result


def test_start_and_poll_completes(tmp_path, monkeypatch):
    import codex_async_mcp.job_manager as jm
    import codex_async_mcp.config as cfg

    monkeypatch.setattr(jm, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(cfg, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(cfg, "CODEX_BIN", "echo")

    # Patch _job_dir to use tmp_path
    original_job_dir = jm._job_dir
    monkeypatch.setattr(jm, "_job_dir", lambda job_id: tmp_path / job_id)

    result = jm.start_job("test prompt", str(tmp_path))
    job_id = result["job_id"]

    # Wait for monitor thread to update meta
    deadline = time.time() + 5
    while time.time() < deadline:
        poll = jm.poll_job(job_id)
        if poll["status"] != "running":
            break
        time.sleep(0.1)

    assert poll["status"] in ("done", "error")


def test_list_jobs_empty(tmp_path, monkeypatch):
    import codex_async_mcp.job_manager as jm
    monkeypatch.setattr(jm, "JOBS_DIR", tmp_path)

    result = jm.list_jobs()
    assert result == []


def test_cancel_unknown_job(tmp_path, monkeypatch):
    import codex_async_mcp.job_manager as jm
    monkeypatch.setattr(jm, "JOBS_DIR", tmp_path)

    result = jm.cancel_job("badid")
    assert "error" in result
