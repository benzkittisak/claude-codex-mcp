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
  agent_type      TEXT  — codex | cursor
"""

import sqlite3
import threading
from typing import Optional

from .config import DB_PATH

# SQLite is thread-safe in WAL mode; we serialise writes with a lock for clarity.
_write_lock = threading.Lock()

# Thread-local connection pool — avoids opening/closing a connection per query.
_local = threading.local()


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    """Return a thread-local cached connection (or create one)."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    _local.conn = conn
    return conn


def init_db() -> None:
    """
    Create schema and configure WAL mode.  Safe to call multiple times.

    WAL mode is set here (once, on an exclusive connection) rather than on
    every _get_conn() call so that concurrent reader connections don't race
    on the PRAGMA and produce "database is locked" errors.
    """
    with _write_lock:
        # Use a fresh, direct connection — not the cached one — because
        # init_db() closes the connection after setup.
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        try:
            # WAL mode makes reads non-blocking; set it once at init time.
            conn.execute("PRAGMA journal_mode=WAL")
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
                    finished_at     TEXT,
                    agent_type      TEXT NOT NULL DEFAULT 'codex'
                )
            """)
            
            # Migration for existing databases
            try:
                conn.execute("ALTER TABLE jobs ADD COLUMN agent_type TEXT NOT NULL DEFAULT 'codex'")
            except sqlite3.OperationalError:
                pass  # Column already exists
                
            # Composite index speeds up the most frequent queries:
            # db_get_next_pending, db_get_running, db_count_status.
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_jobs_status_created
                ON jobs (status, created_at)
            """)
            conn.commit()
        finally:
            conn.close()

    # Invalidate any cached connection on this thread (it may point to
    # a previous DB_PATH, e.g. when tests swap the path between runs).
    _local.conn = None


def reset_pool() -> None:
    """Invalidate the thread-local cached connection.

    Called by tests when DB_PATH is monkeypatched to a fresh tmp file.
    """
    old = getattr(_local, "conn", None)
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
    _local.conn = None


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

def db_insert_job(
    job_id: str,
    prompt: str,
    cwd: str,
    approval_policy: str,
    created_at: str,
    agent_type: str = "codex",
) -> None:
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            """INSERT INTO jobs
               (job_id, status, prompt, cwd, approval_policy, created_at, agent_type)
               VALUES (?, 'pending', ?, ?, ?, ?, ?)""",
            (job_id, prompt, cwd, approval_policy, created_at, agent_type),
        )
        conn.commit()


def db_update_job(job_id: str, **kwargs) -> None:
    """Update arbitrary columns for a job. kwargs maps column → value."""
    if not kwargs:
        return
    set_clause = ", ".join(f"{col} = ?" for col in kwargs)
    values = list(kwargs.values()) + [job_id]
    with _write_lock:
        conn = _get_conn()
        conn.execute(f"UPDATE jobs SET {set_clause} WHERE job_id = ?", values)
        conn.commit()


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

def db_get_job(job_id: str) -> Optional[dict]:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    return dict(row) if row else None


def db_get_next_pending() -> Optional[dict]:
    """Return the oldest pending job (FIFO), or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM jobs WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def db_get_running() -> Optional[dict]:
    """Return the currently running job, or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM jobs WHERE status = 'running' LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def db_list_jobs(limit: int = 20) -> list[dict]:
    """Return jobs ordered newest-first."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def db_count_status(status: str) -> int:
    conn = _get_conn()
    return conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status = ?", (status,)
    ).fetchone()[0]
