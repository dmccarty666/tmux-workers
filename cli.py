#!/usr/bin/env python3
"""
Hermes Tmux Workers — CLI Interface
Provides commands for spawning, tracking, and managing tmux workers.

Usage:
  tmux-workers enqueue <title> <body>          # Add task to queue
  tmux-workers list                            # Show all sessions + tasks
  tmux-workers attach <session>                # Attach to a worker session
  tmux-workers kill <session>                   # Kill a worker
  tmux-workers status                          # Dashboard summary
  tmux-workers revision <session> <message>     # Send revision feedback to worker
  tmux-workers artifact <task_id> <path>        # Add artifact path to task
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/home/dmccarty/.hermes"))
PROJECT_DIR = HERMES_HOME / "PROJECTS" / "tmux-workers"
DB_PATH = PROJECT_DIR / "db" / "state.db"
QUEUE_DIR = PROJECT_DIR / "queue"
WORKSPACES_DIR = PROJECT_DIR / "workspaces"

sys.path.insert(0, str(PROJECT_DIR))


def get_db():
    import sqlite3
    return sqlite3.connect(DB_PATH)


def cmd_enqueue(title, body, project=None, story_id=None, slug=None, task_type=None):
    """Enqueue a new task via the launcher's REST API (uses naming convention).
    Falls back to direct queue-file write if launcher is unreachable."""
    import urllib.request, urllib.error

    url = "http://localhost:9876/api/tmux-workers/enqueue"
    payload = {"title": title, "body": body, "project": project, "story_id": story_id, "slug": slug}
    if task_type:
        payload["task_type"] = task_type
    data = json.dumps(payload).encode()

    try:
        req = urllib.request.Request(url, data=data, method="POST",
                                      headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            task_id = result.get("task_id")
            display_name = result.get("display_name", title)
            print(f"✓ Enqueued: {task_id}")
            print(f"  Title: {display_name}")
            if project or story_id:
                print(f"  Context: {story_id or project}")
            print(f"  Dashboard: http://localhost:9876/dashboard")
            return task_id
    except (urllib.error.HTTPError) as e:
        if e.code == 429:
            # Capacity reached — launcher correctly rejected, but we can write
            # directly to queue so the task still gets dispatched when a slot opens.
            # NOTE: use err_body (not body) to avoid shadowing the task body string,
            # which the direct-write fallback below needs to bind to SQLite.
            err_body = json.loads(e.read())
            print(f"[!] Workers at capacity ({err_body.get('active_workers')}/{err_body.get('max_workers')})")
            print(f"    Queuing via fallback — will dispatch when slot opens")
        else:
            print(f"[!] HTTP {e.code}: {e.read().decode()}")
            return None
    except (urllib.error.URLError, ConnectionRefusedError, TimeoutError) as e:
        print(f"[!] API unreachable ({e}) — falling back to direct queue write...")

    # ── Fallback: direct queue-file write ───────────────────────────────
    # Re-generate the task_id using the naming convention
    import sqlite3
    sys.path.insert(0, str(PROJECT_DIR))
    from launcher import build_task_id, init_db

    conn = sqlite3.connect(DB_PATH)
    task_id, display_name, slug_used, seq = build_task_id(
        conn, title, slug=slug, project=project, story_id=story_id
    )

    task_file = QUEUE_DIR / f"{task_id}.json"
    task_file.write_text(json.dumps({
        "task_id": task_id, "title": title, "body": body,
        "project": project, "story_id": story_id, "slug": slug_used,
        "display_name": display_name, "seq": seq,
        "task_type": task_type or ""
    }))

    c = conn.cursor()
    c.execute("""INSERT OR REPLACE INTO tasks
        (id, title, body, project, story_id, slug, seq, display_name, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued')""",
        (task_id, title, body, project, story_id, slug_used, seq, display_name))
    c.execute("INSERT INTO history (task_id, event, detail) VALUES (?, 'enqueued', ?)",
              (task_id, json.dumps({"title": title, "project": project, "story_id": story_id})))
    conn.commit()
    conn.close()

    print(f"✓ Enqueued (fallback): {task_id}")
    print(f"  Title: {display_name}")
    if project or story_id:
        print(f"  Context: {story_id or project}")
    print(f"  Dashboard: http://localhost:9876/dashboard")
    return task_id


def cmd_list():
    """List active sessions and queued tasks."""
    import sqlite3
    conn = get_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    print("=" * 60)
    print("ACTIVE SESSIONS")
    print("=" * 60)
    c.execute("SELECT * FROM sessions WHERE status IN ('idle', 'working', 'blocked') ORDER BY created_at DESC")
    sessions = c.fetchall()
    if not sessions:
        print("  (no active sessions)")
    for s in sessions:
        age = time.strftime("%H:%M:%S", time.localtime(time.time() - (time.time() - s['created_at'])))
        print(f"  [{s['status']:8}] {s['id']}  task={s['task_id'] or '—'}  age={age}")
    
    print()
    print("=" * 60)
    print("QUEUE")
    print("=" * 60)
    c.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY created_at DESC")
    tasks = c.fetchall()
    if not tasks:
        print("  (queue empty)")
    for t in tasks:
        print(f"  {t['id']}: {t['title']}")
    
    print()
    print("=" * 60)
    print("RECENTLY COMPLETED")
    print("=" * 60)
    c.execute("SELECT * FROM tasks WHERE status='done' ORDER BY completed_at DESC LIMIT 10")
    done = c.fetchall()
    if not done:
        print("  (no completed tasks)")
    for t in done:
        summary = (t['result_summary'] or '—')[:60]
        print(f"  ✓ {t['id']}: {t['title']}")
        print(f"    → {summary}")
    
    conn.close()


def cmd_attach(session):
    """Open tmux attach for a session."""
    print(f"Attaching to {session}...")
    subprocess.run(["tmux", "attach-session", "-t", session])


def cmd_kill(session):
    """Kill a tmux session."""
    result = subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True, text=True)
    if result.returncode == 0:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE sessions SET status='dead' WHERE id=?", (session,))
        c.execute("INSERT INTO history (task_id, event, detail) VALUES (?, 'killed', ?)",
                  (session, json.dumps({"session": session})))
        conn.commit()
        conn.close()
        print(f"✓ Session killed: {session}")
    else:
        print(f"✗ Failed to kill session: {result.stderr}")


def cmd_revision(session, message):
    """Send revision feedback to a worker via revision.txt."""
    import sqlite3
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT task_id FROM sessions WHERE id=?", (session,))
    row = c.fetchone()
    if not row:
        print(f"✗ Session not found: {session}")
        conn.close()
        return
    
    task_id = row[0]
    workspace = WORKSPACES_DIR / task_id
    revision_file = workspace / "revision.txt"
    revision_file.write_text(message)
    
    # Send a signal to the tmux session
    subprocess.run(["tmux", "send-keys", "-t", session, "echo 'revision received'", "Enter"])
    
    c.execute("INSERT INTO history (task_id, event, detail) VALUES (?, 'revision_sent', ?)",
              (task_id, json.dumps({"session": session, "message": message[:100]})))
    conn.commit()
    conn.close()
    
    print(f"✓ Revision sent to {session}")


def cmd_status():
    """Show status summary."""
    conn = get_db()
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) FROM sessions WHERE status='working'")
    working = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM sessions WHERE status='idle'")
    idle = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM tasks WHERE status='queued'")
    queued = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM tasks WHERE status='done'")
    done = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM tasks WHERE status='failed'")
    failed = c.fetchone()[0]
    
    print("Tmux Workers Status")
    print(f"  Working:  {working}")
    print(f"  Idle:     {idle}")
    print(f"  Queued:   {queued}")
    print(f"  Done:     {done}")
    print(f"  Failed:   {failed}")
    
    conn.close()


def cmd_start():
    """Start the launcher (dispatcher + webhook + API)."""
    import subprocess
    proc = subprocess.Popen(
        [sys.executable, str(PROJECT_DIR / "launcher.py")],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(PROJECT_DIR)
    )
    print(f"✓ Launcher started (PID: {proc.pid})")
    print(f"  Dashboard: http://localhost:9876/dashboard")
    print(f"  Webhook:   http://localhost:9876/webhook/completion")


def main():
    parser = argparse.ArgumentParser(description="Hermes Tmux Workers CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)
    
    sub.add_parser("list", help="List sessions and tasks")
    sub.add_parser("status", help="Show status summary")
    sub.add_parser("start", help="Start the launcher")
    
    p_enqueue = sub.add_parser("enqueue", help="Enqueue a task")
    p_enqueue.add_argument("title", help="Task title")
    p_enqueue.add_argument("body", help="Task body/description", nargs="+")
    p_enqueue.add_argument("--project", "-p", help="Project name (e.g. financial-agent)")
    p_enqueue.add_argument("--story", "-s", help="Story ID (e.g. F-1.1, bug-47)")
    p_enqueue.add_argument("--type", "-t", choices=["nl", "bash"],
                           help="Execution mode override. nl=LLM agent, bash=shell script. "
                                "If omitted, bootstrap.sh auto-detects (20-line heuristic).")
    
    p_attach = sub.add_parser("attach", help="Attach to session")
    p_attach.add_argument("session", help="Session name")
    
    p_kill = sub.add_parser("kill", help="Kill a session")
    p_kill.add_argument("session", help="Session name")
    
    p_rev = sub.add_parser("revision", help="Send revision to worker")
    p_rev.add_argument("session", help="Session name")
    p_rev.add_argument("message", help="Revision message", nargs="+")
    
    args = parser.parse_args()
    
    if args.cmd == "list":
        cmd_list()
    elif args.cmd == "status":
        cmd_status()
    elif args.cmd == "start":
        cmd_start()
    elif args.cmd == "enqueue":
        title = args.title
        body = " ".join(args.body)
        cmd_enqueue(title, body, project=args.project, story_id=args.story, task_type=args.type)
    elif args.cmd == "attach":
        cmd_attach(args.session)
    elif args.cmd == "kill":
        cmd_kill(args.session)
    elif args.cmd == "revision":
        message = " ".join(args.message)
        cmd_revision(args.session, message)


if __name__ == "__main__":
    main()