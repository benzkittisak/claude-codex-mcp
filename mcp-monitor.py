import sqlite3
import time
import os
import sys
from pathlib import Path

DB_PATH = Path.home() / ".agent-async" / "queue.db"
JOBS_DIR = Path.home() / ".agent-async" / "jobs"

def get_db_connection():
    try:
        if not DB_PATH.exists():
            return None
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None

def main():
    # Initial clear screen
    os.system('cls' if os.name == 'nt' else 'clear')
    
    while True:
        out = []
        out.append("="*70)
        out.append(" 🚀 MCP Agent Monitor (Real-time Dashboard) ")
        out.append("="*70)
        
        conn = get_db_connection()
        if not conn:
            out.append("❌ Cannot connect to database (queue.db not found).")
            out.append("   Please start a job via MCP to initialize the database.")
            
            # Print everything at once
            sys.stdout.write('\033[H' + '\n'.join(out) + '\033[J')
            sys.stdout.flush()
            time.sleep(2)
            continue
            
        try:
            # Get running job
            running_job = conn.execute("SELECT * FROM jobs WHERE status = 'running' LIMIT 1").fetchone()
            
            if running_job:
                agent = running_job['agent_type'].upper()
                job_id = running_job['job_id']
                prompt = running_job['prompt'].split('\n')[0][:80]
                
                out.append(f"🟢 [RUNNING] Agent: {agent} | Job ID: {job_id}")
                out.append(f"📝 Prompt: {prompt}...")
                out.append("-" * 70)
                
                # Show live output
                output_file = JOBS_DIR / job_id / "output.txt"
                if output_file.exists():
                    out.append("📺 LIVE OUTPUT (Last 15 lines):")
                    try:
                        with open(output_file, 'r', encoding='utf-8', errors='replace') as f:
                            lines = f.readlines()
                            for line in lines[-15:]:
                                # Remove trailing newline from file and append
                                out.append("   " + line.rstrip())
                    except Exception as e:
                        out.append(f"   [Error reading output: {e}]")
                else:
                    out.append("   [No output generated yet]")
            else:
                out.append("💤 No active jobs running. Agents are waiting for tasks...")
                
            out.append("="*70)
            
            # Show pending
            pending_count = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'pending'").fetchone()[0]
            if pending_count > 0:
                out.append(f"⏳ Pending tasks in queue: {pending_count}")
                
            # Show recently completed
            recent = conn.execute("SELECT * FROM jobs WHERE status IN ('done', 'error') ORDER BY finished_at DESC LIMIT 5").fetchall()
            if recent:
                out.append("\n✅ Recently Finished:")
                for r in recent:
                    status_icon = "✔️" if r['status'] == 'done' else "❌"
                    agent = r['agent_type'].upper()
                    summary = r['prompt'].replace('\n', ' ')[:50]
                    out.append(f"  {status_icon} [{agent}] {r['job_id']} - {summary}...")
                    
        except Exception as e:
            out.append(f"Error querying data: {e}")
        finally:
            conn.close()
            
        out.append("\n(Press Ctrl+C to exit. Auto-updating every 1 second...)")
        
        # Write everything to the screen in exactly one operation
        # \033[H moves cursor to top-left
        # \033[J clears anything below the last line we printed
        sys.stdout.write('\033[H' + '\n'.join(out) + '\n\033[J')
        sys.stdout.flush()
        
        time.sleep(1)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nExiting monitor...")
