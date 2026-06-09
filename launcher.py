#!/usr/bin/env python3
"""
Tmux Workers — Unified Launcher
Starts the dispatcher daemon, webhook receiver, and exposes REST API for dashboard.

Usage:
    python3 launcher.py                    # start all
    python3 launcher.py --dispatcher-only   # dispatcher only
    python3 launcher.py --webhook-only      # webhook only
    python3 launcher.py --api-only         # API + dashboard only
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/home/dmccarty/.hermes"))
PROJECT_DIR = HERMES_HOME / "PROJECTS" / "tmux-workers"
DB_PATH = PROJECT_DIR / "db" / "state.db"
QUEUE_DIR = PROJECT_DIR / "queue"
WORKSPACES_DIR = PROJECT_DIR / "workspaces"

sys.path.insert(0, str(PROJECT_DIR / "dispatcher"))
sys.path.insert(0, str(PROJECT_DIR / "webhook"))


# ── Import from submodules ──────────────────────────────────────────

def init_db():
    import sqlite3
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY, task_id TEXT, status TEXT DEFAULT 'idle',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, heartbeat_at DATETIME, result_summary TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
        project TEXT, story_id TEXT, slug TEXT,
        seq INTEGER DEFAULT 1,
        display_name TEXT,
        status TEXT DEFAULT 'queued', assigned_session TEXT, result_summary TEXT,
        artifacts TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, completed_at DATETIME)""")
    c.execute("""CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, event TEXT NOT NULL,
        detail TEXT, at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    # Slug seq table for auto-incrementing per slug
    c.execute("""CREATE TABLE IF NOT EXISTS slug_seq (
        slug TEXT PRIMARY KEY, seq INTEGER DEFAULT 0)""")

    # Migration: add new columns if tasks table exists without them
    for col, col_type in [("project", "TEXT"), ("story_id", "TEXT"), ("slug", "TEXT"),
                          ("seq", "INTEGER DEFAULT 1"), ("display_name", "TEXT")]:
        try:
            c.execute(f"ALTER TABLE tasks ADD COLUMN {col} {col_type}")
            conn.commit()
        except Exception:
            pass  # column already exists

    conn.commit()
    return conn


# ── Naming convention helpers ───────────────────────────────────────

def _make_slug(title: str) -> str:
    """Derive a short slug from a title — first 3 words, lowercase, sanitized."""
    words = title.strip().split()
    slug = "-".join(words[:3]).lower()
    slug = re.sub(r'[^a-z0-9-]', '', slug)
    slug = re.sub(r'-+', '-', slug).strip('-')
    return slug or "task"


def _next_seq(conn, slug: str) -> int:
    """Atomically increment and return the next seq for a given slug."""
    c = conn.cursor()
    c.execute("SELECT seq FROM slug_seq WHERE slug=?", (slug,))
    row = c.fetchone()
    seq = (row[0] + 1) if row else 1
    c.execute("INSERT OR REPLACE INTO slug_seq (slug, seq) VALUES (?, ?)", (slug, seq))
    conn.commit()
    return seq


def build_task_id(conn, title: str, slug: str = None, project: str = None, story_id: str = None) -> tuple[str, str, str, int]:
    """
    Build a human-readable task ID and display name.

    Returns (task_id, display_name, slug, seq) where:
      task_id      = tw_<slug>_<nnn>        (used for session name + DB key + tmux session)
      display_name = [story-id] title      (scannable label for dashboard)
      slug         = the slug used
      seq          = auto-increment per slug
    """
    slug = slug or _make_slug(title)
    seq = _next_seq(conn, slug)

    task_id = f"tw_{slug}_{seq:03d}"

    # Build scannable display name
    if story_id:
        display_name = f"{story_id}: {title}"
    elif project:
        display_name = f"[{project}] {title}"
    else:
        display_name = title

    return task_id, display_name, slug, seq


# ── Config (can be overridden) ───────────────────────────────────────

MAX_CONCURRENT_WORKERS = 3
MAX_TASK_DURATION_MINUTES = 60  # hard limit per task; 0 = no limit
WORKSPACE_TTL_DAYS = 7         # workspaces older than this are purged; 0 = no purge
MAX_LOG_BYTES = 5 * 1024 * 1024  # 5 MB log rotation
LOG_BACKUP_COUNT = 3


# ── Secrets detection ─────────────────────────────────────────────────

SECRET_PATTERNS = [
    (re.compile(r'(?i)(api[_-]?key|apikey)\s*[:=]\s*["\']?[\w\-]{16,}["\']?'), "API_KEY"),
    (re.compile(r'(?i)(secret[_-]?key|access[_-]?token|auth[_-]?token|bearer)\s*[:=]\s*["\']?[\w\-]{16,}["\']?'), "SECRET_KEY/TOKEN"),
    (re.compile(r'(?i)(password|passwd|pwd)\s*[:=]\s*["\']?[^"\']{6,}["\']?'), "PASSWORD"),
    (re.compile(r'-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----'), "PRIVATE_KEY"),
]

def scrub_secrets(text: str) -> list[str]:
    """Return list of secret type names detected in text."""
    return [label for pat, label in SECRET_PATTERNS if pat.search(text)]

def run_dispatcher():
    """Run dispatcher loop in a background thread."""
    import sqlite3
    from datetime import datetime, timedelta

    conn = init_db()
    BOOTSTRAP_SCRIPT = PROJECT_DIR / "workers" / "bootstrap.sh"
    START_TIME = time.time()

    print("[launcher] dispatcher thread started")

    # ── Orphan session reclamation ─────────────────────────────────────
    # On startup, any tmux session that looks like a worker session but has no
    # corresponding DB entry is re-registered so the dispatcher can track it.
    try:
        res = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                             capture_output=True, text=True)
        if res.returncode == 0:
            for session_name in res.stdout.strip().split("\n"):
                if session_name.startswith("tw_"):
                    c = conn.cursor()
                    c.execute("SELECT task_id FROM sessions WHERE id=?", (session_name,))
                    if not c.fetchone():
                        # Orphaned session from a previous launcher instance — reclaim
                        c.execute("""INSERT OR IGNORE INTO sessions (id, task_id, status, heartbeat_at)
                                     VALUES (?, NULL, 'working', CURRENT_TIMESTAMP)""",
                                  (session_name,))
                        print(f"[dispatcher] reclaimed orphan tmux session: {session_name}")
            conn.commit()
    except Exception as e:
        print(f"[dispatcher] orphan scan skipped: {e}")
    
    def log_event(task_id, event, detail=None):
        c = conn.cursor()
        c.execute("INSERT INTO history (task_id, event, detail) VALUES (?, ?, ?)",
                  (task_id, event, json.dumps(detail) if detail else None))
        conn.commit()
    
    def get_active_count():
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM sessions WHERE status='working'")
        return c.fetchone()[0]
    
    while True:
        try:
            active = get_active_count()
            available = MAX_CONCURRENT_WORKERS - active
            
            # Scan queue for new tasks
            if QUEUE_DIR.exists():
                for task_file in QUEUE_DIR.glob("*.json"):
                    if available <= 0:
                        break
                    # Skip completion files
                    if task_file.name.startswith(".") or task_file.stem in ("completions",):
                        continue
                    try:
                        data = json.loads(task_file.read_text())
                        task_id = data.get("task_id")
                        if not task_id:
                            continue
                        
                        c = conn.cursor()
                        c.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
                        row = c.fetchone()
                        
                        if row and row[0] == "queued":
                            workspace = WORKSPACES_DIR / task_id
                            workspace.mkdir(parents=True, exist_ok=True)
                            session_id = task_id  # task_id IS the session name: tw_<slug>_<nnn>

                            result = subprocess.run(
                                ["tmux", "new-session", "-d", "-s", session_id,
                                 "bash", str(BOOTSTRAP_SCRIPT), task_id, str(workspace), session_id],
                                capture_output=True, text=True
                            )
                            
                            if result.returncode == 0:
                                c.execute("""INSERT OR REPLACE INTO sessions (id, task_id, status, heartbeat_at)
                                              VALUES (?, ?, 'working', CURRENT_TIMESTAMP)""",
                                          (session_id, task_id))
                                c.execute("UPDATE tasks SET status='assigned', assigned_session=? WHERE id=?",
                                          (session_id, task_id))
                                conn.commit()
                                log_event(task_id, "assigned", {"session": session_id})
                                print(f"[dispatcher] spawned {session_id} for {task_id}")
                                available -= 1
                            else:
                                print(f"[dispatcher] tmux spawn failed: {result.stderr}")
                    except Exception as e:
                        print(f"[dispatcher] error: {e}")
            
            # Check for completion files from workers
            completion_queue = QUEUE_DIR / "completions"
            if completion_queue.exists():
                for cf in completion_queue.glob("*.json"):
                    try:
                        payload = json.loads(cf.read_text())
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
                            log_event(task_id, "completed", payload)
                            print(f"[dispatcher] task {task_id} completed")
                        elif status == "blocked":
                            c.execute("UPDATE sessions SET status='blocked' WHERE id=?", (session,))
                            log_event(task_id, "blocked", {"reason": payload.get("blocked_reason")})
                        
                        conn.commit()
                        cf.unlink()  # remove after processing
                    except Exception as e:
                        print(f"[dispatcher] completion processing error: {e}")
            
            # Check for stale sessions (heartbeat timeout, or session died before starting)
            c.execute("""SELECT s.id, s.task_id FROM sessions s
                         LEFT JOIN tasks t ON s.task_id = t.id
                         WHERE s.status='working'
                         AND (
                             s.heartbeat_at < datetime('now', '-10 minutes')
                             OR (t.created_at < datetime('now', '-2 minutes')
                                 AND NOT EXISTS (SELECT 1 FROM sessions WHERE id=s.id AND heartbeat_at IS NOT NULL))
                         )""")
            for session_id, task_id in c.fetchall():
                # Check if the workspace has a .started marker (session actually ran)
                workspace = WORKSPACES_DIR / session_id
                started_file = workspace / ".started"
                died_immediately = not started_file.exists()

                reason = "session died immediately after spawn" if died_immediately else "heartbeat timeout"
                c.execute("UPDATE sessions SET status='dead' WHERE id=?", (session_id,))
                c.execute("UPDATE tasks SET status='failed' WHERE id=?", (task_id,))
                log_event(task_id, "failed", {"reason": reason, "session": session_id})
                print(f"[dispatcher] session {session_id} marked dead ({reason})")
                # Clean up tmux session
                subprocess.run(["tmux", "kill-session", "-t", session_id],
                               capture_output=True, text=True)
            conn.commit()

            # Check for task max-duration exceeded
            if MAX_TASK_DURATION_MINUTES > 0:
                c.execute("""SELECT s.id, s.task_id FROM sessions s
                             JOIN tasks t ON s.task_id = t.id
                             WHERE s.status='working'
                             AND t.created_at < datetime('now', ?)""",
                          (f"-{MAX_TASK_DURATION_MINUTES} minutes",))
                for session_id, task_id in c.fetchall():
                    c.execute("UPDATE sessions SET status='dead' WHERE id=?", (session_id,))
                    c.execute("UPDATE tasks SET status='failed' WHERE id=?", (task_id,))
                    log_event(task_id, "failed", {"reason": f"max duration {MAX_TASK_DURATION_MINUTES}m exceeded", "session": session_id})
                    print(f"[dispatcher] session {session_id} marked dead (max duration exceeded)")
                    # Kill the tmux session
                    subprocess.run(["tmux", "kill-session", "-t", session_id],
                                   capture_output=True, text=True)
                conn.commit()

            # Workspace TTL cleanup
            if WORKSPACE_TTL_DAYS > 0 and WORKSPACES_DIR.exists():
                cutoff = time.time() - (WORKSPACE_TTL_DAYS * 86400)
                for ws in WORKSPACES_DIR.iterdir():
                    if ws.is_dir() and ws.stat().st_mtime < cutoff:
                        import shutil
                        try:
                            shutil.rmtree(ws)
                            print(f"[dispatcher] purged old workspace: {ws.name}")
                        except Exception as e:
                            print(f"[dispatcher] failed to purge workspace {ws.name}: {e}")

            # ── Stale tmux session cleanup (done sessions idle > 2h) ──────
            done_timeout = 7200  # 2 hours
            r = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                              capture_output=True, text=True)
            for session_name in r.stdout.strip().split("\n"):
                if not session_name:
                    continue
                c.execute("SELECT status, heartbeat_at FROM sessions WHERE id=?",
                         (session_name,))
                row = c.fetchone()
                if row and row[0] == "done":
                    try:
                        hb = datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S")
                        age = (datetime.utcnow() - hb).total_seconds()
                        if age > done_timeout:
                            subprocess.run(["tmux", "kill-session", "-t", session_name],
                                          capture_output=True, text=True)
                            c.execute("UPDATE sessions SET status='archived' WHERE id=?",
                                     (session_name,))
                            print(f"[cleanup] killed stale done session: {session_name} ({int(age/3600)}h idle)")
                    except (ValueError, TypeError):
                        pass

            # ── Queue file cleanup (remove for tasks > 7 days old) ────────
            if QUEUE_DIR.exists():
                for qf in QUEUE_DIR.glob("*.json"):
                    try:
                        if qf.stat().st_mtime < (time.time() - WORKSPACE_TTL_DAYS * 86400):
                            qf.unlink()
                    except Exception:
                        pass

            # ── Completion file cleanup (> 7 days) ────────────────────────
            completions_dir = QUEUE_DIR / "completions"
            if completions_dir.exists():
                for cf in completions_dir.glob("*.json"):
                    try:
                        if cf.stat().st_mtime < (time.time() - WORKSPACE_TTL_DAYS * 86400):
                            cf.unlink()
                    except Exception:
                        pass

            # ── DB maintenance (VACUUM daily, prune old history) ──────────
            now = time.time()
            last_vacuum = float(os.environ.get("HERMES_LAST_VACUUM", "0") or "0")
            if now - last_vacuum > 86400:  # 24h
                c.execute("DELETE FROM history WHERE at < datetime('now', '-30 days')")
                deleted = c.rowcount
                if deleted:
                    print(f"[cleanup] pruned {deleted} old history rows")
                conn.commit()
                c.execute("VACUUM")
                conn.commit()
                os.environ["HERMES_LAST_VACUUM"] = str(now)
                print(f"[cleanup] DB vacuumed")

            time.sleep(5)
        except Exception as e:
            print(f"[dispatcher] loop error: {e}")
            time.sleep(5)


# ── Webhook receiver (inline HTTP server) ────────────────────────────

# ── Combined handler: API + Webhook on same port ──────────────────────

class CombinedHandler(SimpleHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/webhook/completion":
            self.handle_webhook_completion()
            return
        if self.path == "/api/tmux-workers/enqueue":
            self.handle_enqueue()
            return
        if self.path == "/api/tmux-workers/dispatcher/start":
            self.send_json({"status": "dispatcher_running"})
            return
        if self.path.startswith("/api/tmux-workers/sessions/") and self.path.endswith("/kill"):
            session_id = self.path.split("/")[-2]
            self.handle_kill_session(session_id)
            return
        self.send_error(404, "Not found")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/api/tmux-workers", "/api/tmux-workers/"):
            self.send_json(self.get_status())
            return
        if path == "/health":
            self.send_json(self.get_health())
            return
        if path in ("/dashboard", "/"):
            dashboard_path = PROJECT_DIR / "dashboard" / "index.html"
            if dashboard_path.exists():
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(dashboard_path.read_bytes())
            else:
                self.send_error(404, "Dashboard not found")
            return
        # Session info (for attach)
        if path.startswith("/api/tmux-workers/sessions/"):
            session_id = path.split("/")[-1]
            self.send_json(self.get_session_info(session_id))
            return
        self.send_error(404, "Not found")

    # ── Webhook ──────────────────────────────────────────────────────────
    def handle_webhook_completion(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return
        payload["received_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        completion_queue = QUEUE_DIR / "completions"
        completion_queue.mkdir(parents=True, exist_ok=True)
        completion_file = completion_queue / f"{payload.get('task_id', 'unknown')}.json"
        completion_file.write_text(json.dumps(payload, indent=2))
        print(f"[webhook] received completion for {payload.get('task_id')}")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"received": True}).encode())

    # ── API ──────────────────────────────────────────────────────────────
    def get_health(self):
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM sessions WHERE status='working'")
        workers = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM tasks WHERE status='queued'")
        queued = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM tasks WHERE status='done'")
        done = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM tasks WHERE status='failed'")
        failed = c.fetchone()[0]
        conn.close()
        return {
            "status": "ok",
            "workers_active": workers,
            "workers_max": MAX_CONCURRENT_WORKERS,
            "queue_depth": queued,
            "tasks_done": done,
            "tasks_failed": failed,
        }

    def get_status(self):
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM sessions ORDER BY created_at DESC LIMIT 50")
        sessions = [dict(row) for row in c.fetchall()]
        c.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 100")
        tasks = [dict(row) for row in c.fetchall()]
        c.execute("SELECT * FROM history ORDER BY at DESC LIMIT 100")
        history = [dict(row) for row in c.fetchall()]
        conn.close()
        return {"sessions": sessions, "tasks": tasks, "history": history}

    def get_session_info(self, session_id):
        """Return info for a session, including attach command."""
        import subprocess
        result = {"session_id": session_id, "exists": False, "attach_cmd": None}
        try:
            r = subprocess.run(["tmux", "has-session", "-t", session_id],
                              capture_output=True, text=True)
            if r.returncode == 0:
                result["exists"] = True
                result["attach_cmd"] = f"tmux attach -t {session_id}"
        except Exception:
            pass
        return result

    def handle_kill_session(self, session_id):
        """Kill a tmux session and update DB."""
        import subprocess, sqlite3
        killed = False
        try:
            subprocess.run(["tmux", "kill-session", "-t", session_id],
                          capture_output=True, text=True)
            killed = True
        except Exception:
            pass
        # Update DB regardless — session is gone or going
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("UPDATE sessions SET status='killed' WHERE id=?", (session_id,))
            conn.commit()
            conn.close()
        except Exception:
            pass
        self.send_json({"killed": killed, "session_id": session_id})

    def handle_enqueue(self):
        import sqlite3
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        data = json.loads(body)

        title = data.get("title", "Untitled")
        body_text = data.get("body", "")
        project = data.get("project")
        story_id = data.get("story_id")
        slug = data.get("slug")
        goal = data.get("goal", "")
        goal_max_turns = int(data.get("goal_max_turns", 5))

        # ── Secrets scan ──────────────────────────────────────────────────
        secret_types = scrub_secrets(body_text)
        if secret_types:
            print(f"[launcher] WARNING: task body contains potential secrets: {secret_types}")

        # ── Concurrent limit ──────────────────────────────────────────────
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM sessions WHERE status='working'")
        active_workers = c.fetchone()[0]
        if active_workers >= MAX_CONCURRENT_WORKERS:
            conn.close()
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": "concurrent_limit_reached",
                "message": f"Max workers ({MAX_CONCURRENT_WORKERS}) active. Task queued, will dispatch when a slot opens.",
                "active_workers": active_workers,
                "max_workers": MAX_CONCURRENT_WORKERS
            }).encode())
            return

        # Build task_id using naming convention
        task_id, display_name, slug_used, seq = build_task_id(
            conn, title, slug=slug, project=project, story_id=story_id
        )

        # Write task file for dispatcher
        task_file = QUEUE_DIR / f"{task_id}.json"
        task_file.write_text(json.dumps({
            "task_id": task_id, "title": title, "body": body_text,
            "project": project, "story_id": story_id, "slug": slug_used,
            "display_name": display_name, "seq": seq,
            "goal": goal, "goal_max_turns": goal_max_turns
        }))

        # Insert into DB with all metadata
        c = conn.cursor()
        c.execute("""INSERT OR REPLACE INTO tasks
            (id, title, body, project, story_id, slug, seq, display_name, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued')""",
            (task_id, title, body_text, project, story_id, slug_used, seq, display_name))
        c.execute("INSERT INTO history (task_id, event, detail) VALUES (?, 'enqueued', ?)",
                  (task_id, json.dumps({"title": title, "project": project, "story_id": story_id})))
        conn.commit()
        conn.close()

        print(f"[launcher] enqueued {task_id}: {display_name}")
        self.send_json({"task_id": task_id, "display_name": display_name, "status": "enqueued"})

    def send_json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())


# ── Main ──────────────────────────────────────────────────────────────

def main():
    import logging
    from logging.handlers import RotatingFileHandler

    parser = argparse.ArgumentParser(description="Tmux Workers launcher")
    parser.add_argument("--port", type=int, default=9876, help="API/Webhook port (default: 9876)")
    parser.add_argument("--dispatcher-only", action="store_true")
    parser.add_argument("--webhook-only", action="store_true")
    parser.add_argument("--api-only", action="store_true")
    args = parser.parse_args()

    # ── Log rotation ────────────────────────────────────────────────────
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    log_path = PROJECT_DIR / "logs" / "launcher.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(log_path, maxBytes=MAX_LOG_BYTES,
                                  backupCount=LOG_BACKUP_COUNT)
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    # Remove any existing handlers (e.g. default stderr) and add rotating one
    for h in root_logger.handlers[:]:
        root_logger.removeHandler(h)
    root_logger.addHandler(handler)
    logging.info("Tmux Workers starting")
    
    # Ensure directories exist
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    (QUEUE_DIR / "completions").mkdir(exist_ok=True)
    init_db()
    
    print(f"[launcher] Tmux Workers starting")
    print(f"[launcher] Project dir: {PROJECT_DIR}")
    print(f"[launcher] Queue dir: {QUEUE_DIR}")
    
    # Start dispatcher in background thread
    if not args.webhook_only and not args.api_only:
        dispatcher_thread = threading.Thread(target=run_dispatcher, daemon=True)
        dispatcher_thread.start()
        print("[launcher] dispatcher started")
    
    # Start webhook + API server
    server_address = ("", args.port)
    httpd = HTTPServer(server_address, CombinedHandler)
    
    print(f"[launcher] HTTP server listening on port {args.port}")
    print(f"[launcher] Dashboard: http://localhost:{args.port}/dashboard")
    print(f"[launcher] Webhook: http://localhost:{args.port}/webhook/completion")
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[launcher] shutting down...")
        sys.exit(0)


if __name__ == "__main__":
    main()