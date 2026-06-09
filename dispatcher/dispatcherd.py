#!/usr/bin/env python3
"""
Tmux Worker Dispatcher Daemon
Watches queue/ for new task files, spawns tmux workers, tracks state in SQLite.
"""
import os
import json
import sqlite3
import subprocess
import time
import signal
import sys
from pathlib import Path
from datetime import datetime, timedelta
from threading import Thread

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/home/dmccarty/.hermes"))
PROJECT_DIR = HERMES_HOME / "PROJECTS" / "tmux-workers"
QUEUE_DIR = PROJECT_DIR / "queue"
WORKSPACES_DIR = PROJECT_DIR / "workspaces"
DB_PATH = PROJECT_DIR / "db" / "state.db"
BOOTSTRAP_SCRIPT = PROJECT_DIR / "workers" / "bootstrap.sh"

MAX_CONCURRENT_WORKERS = 3
HEARTBEAT_INTERVAL = 60  # seconds
STALE_SESSION_THRESHOLD = 600  # seconds


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            task_id TEXT,
            status TEXT DEFAULT 'idle',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            heartbeat_at DATETIME,
            result_summary TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            status TEXT DEFAULT 'queued',
            assigned_session TEXT,
            result_summary TEXT,
            artifacts TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            completed_at DATETIME
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            event TEXT NOT NULL,
            detail TEXT,
            at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn


def log_event(conn, task_id, event, detail=None):
    c = conn.cursor()
    c.execute("INSERT INTO history (task_id, event, detail) VALUES (?, ?, ?)",
              (task_id, event, json.dumps(detail) if detail else None))
    conn.commit()


def get_active_session_count(conn):
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM sessions WHERE status = 'working'")
    return c.fetchone()[0]


def enqueue_task(conn, task_id, title, body):
    """Write task file to queue and create DB record."""
    task_file = QUEUE_DIR / f"{task_id}.json"
    task_data = {"task_id": task_id, "title": title, "body": body}
    task_file.write_text(json.dumps(task_data, indent=2))
    
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO tasks (id, title, body, status) VALUES (?, ?, ?, 'queued')",
              (task_id, title, body))
    conn.commit()
    print(f"[dispatcher] enqueued {task_id}: {title}")


def dispatch_task(conn, task_id):
    """Spawn a tmux worker for the given task."""
    if get_active_session_count(conn) >= MAX_CONCURRENT_WORKERS:
        print(f"[dispatcher] max workers ({MAX_CONCURRENT_WORKERS}) reached, skipping {task_id}")
        return False
    
    task_file = QUEUE_DIR / f"{task_id}.json"
    if not task_file.exists():
        print(f"[dispatcher] task file not found: {task_file}")
        return False
    
    workspace = WORKSPACES_DIR / task_id
    workspace.mkdir(parents=True, exist_ok=True)
    
    # Create tmux session name — use full task_id to avoid collision
    # (Old code used f"worker-{task_id[:8]}" which truncated and caused
    # 4-of-5 concurrent workers to collide on the same session name. See
    # tmux-workers/references/multi-agent-roadmap-2026-06-09.md for history.)
    session_id = task_id

    # Spawn tmux session
    cmd = [
        "tmux", "new-session", "-d", "-s", session_id,
        "bash", str(BOOTSTRAP_SCRIPT),
        task_id, str(workspace), session_id
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[dispatcher] failed to spawn {session_id}: {result.stderr}")
        return False
    
    # Update session and task records
    c = conn.cursor()
    c.execute("""INSERT OR REPLACE INTO sessions (id, task_id, status, heartbeat_at) 
                  VALUES (?, ?, 'working', CURRENT_TIMESTAMP)""",
              (session_id, task_id))
    c.execute("""UPDATE tasks SET status='assigned', assigned_session=? WHERE id=?""",
              (session_id, task_id))
    conn.commit()
    
    log_event(conn, task_id, "assigned", {"session": session_id})
    print(f"[dispatcher] spawned {session_id} for {task_id}")
    return True


def scan_queue(conn):
    """Check queue for new tasks and dispatch up to available slots."""
    if not QUEUE_DIR.exists():
        return
    
    active = get_active_session_count(conn)
    available = MAX_CONCURRENT_WORKERS - active
    
    for task_file in QUEUE_DIR.glob("*.json"):
        if available <= 0:
            break
        try:
            data = json.loads(task_file.read_text())
            task_id = data["task_id"]
            
            c = conn.cursor()
            c.execute("SELECT status FROM tasks WHERE id = ?", (task_id,))
            row = c.fetchone()
            if row and row[0] == "queued":
                if dispatch_task(conn, task_id):
                    available -= 1
        except Exception as e:
            print(f"[dispatcher] error processing {task_file}: {e}")


def check_heartbeats(conn):
    """Mark stale sessions as dead, log as failed."""
    c = conn.cursor()
    c.execute("""SELECT id, task_id FROM sessions 
                 WHERE status='working' 
                 AND heartbeat_at < datetime('now', '-' || ? || ' seconds')""",
              (STALE_SESSION_THRESHOLD,))
    stale = c.fetchall()
    
    for session_id, task_id in stale:
        c.execute("UPDATE sessions SET status='dead' WHERE id=?", (session_id,))
        c.execute("UPDATE tasks SET status='failed' WHERE id=?", (task_id,))
        log_event(conn, task_id, "failed", {"reason": "heartbeat timeout", "session": session_id})
        print(f"[dispatcher] session {session_id} marked dead (heartbeat timeout)")
    
    conn.commit()


def session_completion_handler(conn, payload):
    """
    Called by the webhook endpoint when a worker POSTs completion.
    payload = {task_id, session, status, summary, artifacts, blocked_reason}
    """
    task_id = payload.get("task_id")
    session = payload.get("session")
    status = payload.get("status", "done")
    
    c = conn.cursor()
    
    if status == "done":
        c.execute("""UPDATE tasks SET status='done', result_summary=?, artifacts=?,
                     completed_at=CURRENT_TIMESTAMP WHERE id=?""",
                  (payload.get("summary"), json.dumps(payload.get("artifacts", [])), task_id))
        c.execute("UPDATE sessions SET status='done', result_summary=? WHERE id=?",
                  (payload.get("summary"), session))
        log_event(conn, task_id, "completed", payload)
        print(f"[dispatcher] task {task_id} completed by {session}")
        
    elif status == "blocked":
        c.execute("UPDATE sessions SET status='blocked' WHERE id=?", (session,))
        log_event(conn, task_id, "blocked", {"reason": payload.get("blocked_reason")})
        print(f"[dispatcher] task {task_id} blocked by {session}")
    
    conn.commit()


def rebuild_queue(conn):
    """Re-scan workspaces for any task files that landed without going through queue."""
    for workspace in WORKSPACES_DIR.iterdir():
        if not workspace.is_dir():
            continue
        task_id = workspace.name
        
        c = conn.cursor()
        c.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
        row = c.fetchone()
        
        # If task exists and has workspace artifacts but no session, it's self-completed
        if row and row[0] == "assigned":
            result_file = workspace / "result.json"
            if result_file.exists():
                try:
                    result = json.loads(result_file.read_text())
                    session_completion_handler(conn, {
                        "task_id": task_id,
                        "session": f"worker-{task_id[:8]}",
                        "status": result.get("status", "done"),
                        "summary": result.get("summary", ""),
                        "artifacts": result.get("artifacts", []),
                        "blocked_reason": result.get("blocked_reason")
                    })
                except Exception as e:
                    print(f"[dispatcher] error reading result for {task_id}: {e}")


def run_loop(poll_interval=5):
    """Main dispatch loop."""
    conn = init_db()
    
    print("[dispatcher] starting tmux-worker dispatcher")
    print(f"[dispatcher] queue: {QUEUE_DIR}")
    print(f"[dispatcher] max workers: {MAX_CONCURRENT_WORKERS}")
    
    running = True
    
    def handle_signal(sig, frame):
        nonlocal running
        print("\n[dispatcher] shutting down...")
        running = False
    
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    
    while running:
        try:
            scan_queue(conn)
            check_heartbeats(conn)
            rebuild_queue(conn)
            time.sleep(poll_interval)
        except Exception as e:
            print(f"[dispatcher] loop error: {e}")
            time.sleep(poll_interval)
    
    conn.close()
    print("[dispatcher] stopped")


if __name__ == "__main__":
    run_loop()