"""
SQLite-backed job queue for codex-async-mcp.

Schema (queue.db / jobs table):
  job_id          TEXT PK
  status          TEXT  — pending | running | done | error | cancelled
  prompt          TEXT
  cwd             TEXT
  approval_policy TEXT
  pid             INTEGER (nullable)
  exit_code       INTEGER (nullable)
  created_at      TEXT  — ISO-8601 UTC
  started_at      TEXT  (nullable)
  finished_at     TEXT  (nullable)
"""

import sqlite3
import threading
from typing import Optional

from .config import DB_PATH

# SQLite is thread-safe in WAL mode; we serialise writes with a lock for clarity.
_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create table if it doesn't exist. Safe to call multiple times."""
    with _write_lock:
        conn = _get_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id          TEXT PRIMARY KEY,
                    status          TEXT NOT NULL DEFAULT 'pending',
                    prompt          TEXT NOT NULL,
                    cwd             TEXT NOT NULL,
                    approval_policy TEXT NOT NULL,
                    pid             INTEGER,
                    exit_code       INTEGER,
                    created_at      TEXT NOT NULL,
                    started_at      TEXT,
                    finished_at     TEXT
                )
            """)
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

def db_insert_job(
    job_id: str,
    prompt: str,
    cwd: str,
    approval_policy: str,
    created_at: str,
) -> None:
    with _write_lock:
        conn = _get_conn()
        try:
            conn.execute(
                """INSERT INTO jobs
                   (job_id, status, prompt, cwd, approval_policy, created_at)
                   VALUES (?, 'pending', ?, ?, ?, ?)""",
                (job_id, prompt, cwd, approval_policy, created_at),
            )
            conn.commit()
        finally:
            conn.close()


def db_update_job(job_id: str, **kwargs) -> None:
    """Update arbitrary columns for a job. kwargs maps column → value."""
    if not kwargs:
        return
    set_clause = ", ".join(f"{col} = ?" for col in kwargs)
    values = list(kwargs.values()) + [job_id]
    with _write_lock:
        conn = _get_conn()
        try:
            conn.execute(f"UPDATE jobs SET {set_clause} WHERE job_id = ?", values)
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

def db_get_job(job_id: str) -> Optional[dict]:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def db_get_next_pending() -> Optional[dict]:
    """Return the oldest pending job (FIFO), or None."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def db_get_running() -> Optional[dict]:
    """Return the currently running job, or None."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status = 'running' LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def db_list_jobs(limit: int = 20) -> list[dict]:
    """Return jobs ordered newest-first."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def db_count_status(status: str) -> int:
    conn = _get_conn()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = ?", (status,)
        ).fetchone()[0]
    finally:
        conn.close()
