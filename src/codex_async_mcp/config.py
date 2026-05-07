from pathlib import Path

JOBS_DIR = Path.home() / ".codex-async" / "jobs"
DB_PATH = Path.home() / ".codex-async" / "queue.db"
DEFAULT_APPROVAL_POLICY = "suggest"
CODEX_BIN = "codex"
JOB_TAIL_LINES = 100
MAX_OUTPUT_CHARS = 8000
