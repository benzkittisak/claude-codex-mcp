from pathlib import Path

JOBS_DIR = Path.home() / ".codex-async" / "jobs"
DB_PATH = Path.home() / ".codex-async" / "queue.db"
DEFAULT_APPROVAL_POLICY = "suggest"
CODEX_BIN = "codex"
JOB_TAIL_LINES = 100
MAX_OUTPUT_CHARS = 8000

# If the output file hasn't grown for this many seconds while the process is
# still alive, we assume Codex finished its task but is hanging on cleanup
# (e.g. a dangling docker-exec child).  The monitor will SIGTERM the process
# and mark the job done.
OUTPUT_STALL_TIMEOUT = 60   # seconds
# How often the monitor wakes up to check for stall (also proc.wait timeout).
MONITOR_POLL_INTERVAL = 5   # seconds
