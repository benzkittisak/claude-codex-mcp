import json
import os
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import (
    CODEX_BIN,
    DEFAULT_APPROVAL_POLICY,
    JOBS_DIR,
    JOB_TAIL_LINES,
    MAX_OUTPUT_CHARS,
)

# In-memory registry of active Popen objects (lost on server restart, fallback to PID check)
_active_procs: dict[str, subprocess.Popen] = {}
_lock = threading.Lock()


def _job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def _read_meta(job_id: str) -> Optional[dict]:
    path = _job_dir(job_id) / "meta.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def _write_meta(job_id: str, meta: dict) -> None:
    path = _job_dir(job_id) / "meta.json"
    with open(path, "w") as f:
        json.dump(meta, f, indent=2)


def _read_tail(job_id: str, tail_lines: int = JOB_TAIL_LINES) -> str:
    output_path = _job_dir(job_id) / "output.txt"
    if not output_path.exists():
        return ""
    text = output_path.read_text(errors="replace")
    lines = text.splitlines()
    tail = "\n".join(lines[-tail_lines:])
    if len(tail) > MAX_OUTPUT_CHARS:
        tail = "...(truncated)...\n" + tail[-MAX_OUTPUT_CHARS:]
    return tail


def _monitor(job_id: str, proc: subprocess.Popen) -> None:
    exit_code = proc.wait()
    with _lock:
        meta = _read_meta(job_id)
        if meta and meta["status"] == "running":
            meta["status"] = "done" if exit_code == 0 else "error"
            meta["exit_code"] = exit_code
            meta["finished_at"] = datetime.now(timezone.utc).isoformat()
            _write_meta(job_id, meta)
        _active_procs.pop(job_id, None)


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_job(prompt: str, cwd: str, approval_policy: str = DEFAULT_APPROVAL_POLICY) -> dict:
    job_id = uuid.uuid4().hex[:8]
    job_dir = _job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    output_path = job_dir / "output.txt"

    # Map legacy approval_policy values to codex exec flags (v0.125.0+)
    approval_flags = {
        "suggest": ["-s", "read-only"],
        "auto-edit": ["--full-auto"],
        "full-auto": ["--dangerously-bypass-approvals-and-sandbox"],
    }
    flags = approval_flags.get(approval_policy, ["--full-auto"])
    cmd = [CODEX_BIN, "exec"] + flags + [prompt]

    with open(output_path, "w") as out_file:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=out_file,
            stderr=subprocess.STDOUT,
            cwd=cwd,
        )

    meta = {
        "job_id": job_id,
        "status": "running",
        "prompt": prompt,
        "cwd": cwd,
        "approval_policy": approval_policy,
        "pid": proc.pid,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "exit_code": None,
    }
    _write_meta(job_id, meta)

    with _lock:
        _active_procs[job_id] = proc

    t = threading.Thread(target=_monitor, args=(job_id, proc), daemon=True)
    t.start()

    return {"job_id": job_id, "status": "running", "pid": proc.pid}


def poll_job(job_id: str, tail_lines: int = JOB_TAIL_LINES) -> dict:
    meta = _read_meta(job_id)
    if meta is None:
        return {"error": f"Job '{job_id}' not found"}

    # If meta still says running but monitor thread hasn't updated yet,
    # fall back to PID liveness check (covers server-restart case too).
    if meta["status"] == "running":
        with _lock:
            proc = _active_procs.get(job_id)

        if proc is None:
            pid = meta.get("pid")
            if pid and not _is_pid_alive(pid):
                with _lock:
                    meta = _read_meta(job_id)
                    if meta and meta["status"] == "running":
                        meta["status"] = "done"
                        meta["finished_at"] = datetime.now(timezone.utc).isoformat()
                        _write_meta(job_id, meta)

    meta = _read_meta(job_id)
    output = _read_tail(job_id, tail_lines)

    return {
        "job_id": job_id,
        "status": meta["status"],
        "exit_code": meta.get("exit_code"),
        "output": output,
        "started_at": meta.get("started_at"),
        "finished_at": meta.get("finished_at"),
        "prompt": meta.get("prompt"),
        "cwd": meta.get("cwd"),
    }


def list_jobs(limit: int = 20) -> list[dict]:
    if not JOBS_DIR.exists():
        return []

    results = []
    for job_dir in sorted(JOBS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        meta_path = job_dir / "meta.json"
        if not meta_path.exists():
            continue
        with open(meta_path) as f:
            meta = json.load(f)
        results.append({
            "job_id": meta["job_id"],
            "status": meta["status"],
            "prompt": meta["prompt"][:80] + ("..." if len(meta["prompt"]) > 80 else ""),
            "cwd": meta["cwd"],
            "started_at": meta["started_at"],
            "finished_at": meta.get("finished_at"),
        })
        if len(results) >= limit:
            break

    return results


def cancel_job(job_id: str) -> dict:
    meta = _read_meta(job_id)
    if meta is None:
        return {"error": f"Job '{job_id}' not found"}

    if meta["status"] != "running":
        return {"job_id": job_id, "status": meta["status"], "message": "Job is not running"}

    with _lock:
        proc = _active_procs.get(job_id)

    killed = False
    if proc is not None:
        proc.terminate()
        killed = True
    else:
        pid = meta.get("pid")
        if pid and _is_pid_alive(pid):
            os.kill(pid, 15)  # SIGTERM
            killed = True

    with _lock:
        meta["status"] = "cancelled"
        meta["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_meta(job_id, meta)
        _active_procs.pop(job_id, None)

    return {"job_id": job_id, "status": "cancelled", "killed": killed}
