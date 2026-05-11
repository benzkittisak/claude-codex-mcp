import os
import sys
import time

# Add src to sys.path so we can import without installing
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "src")))

from codex_async_mcp.db import init_db
from codex_async_mcp.job_manager import start_job, poll_job, get_queue_status, cancel_job

def main():
    print("Initializing Database...")
    init_db()
    
    cwd = os.getcwd()
    print(f"Current Directory: {cwd}")
    
    print("\n--- Starting Cursor Job ---")
    result = start_job(prompt="Can you say 'Hello from Cursor CLI' in output?", cwd=cwd, agent_type="cursor", context_files=["dummy_context.py"])
    print("Start Result:", result)
    
    job_id = result.get("job_id")
    if not job_id:
        print("Failed to get job_id!")
        return
        
    print("\n--- Waiting 10 seconds for Cursor agent to process... ---")
    for i in range(10):
        time.sleep(1)
        status = get_queue_status()
        running = status.get("running")
        if running is None:
            break
        print(f"[{i+1}s] Still running...")
    
    print("\n--- Final Job Status ---")
    final = poll_job(job_id)
    print("Status:", final.get("status"))
    print("Exit Code:", final.get("exit_code"))
    print("Output:\n" + final.get("output", ""))

if __name__ == "__main__":
    main()
