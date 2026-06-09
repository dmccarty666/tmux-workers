"""
tmux_workers — Hermes native tool module
Exposes tmux-workers lifecycle as callable functions for Hermes and its sub-agents.

Provides:
  tmux_workers.spawn(title, task_body, model=None) → task_id
  tmux_workers.list()   → {sessions, tasks, history}
  tmux_workers.kill(session_id)
  tmux_workers.revision(session_id, feedback)
  tmux_workers.status(task_id=None)

Usage from Hermes (or any sub-agent):
  tmux_workers.spawn("Analyze this repo", "Find all TODO comments and count them")
"""

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Optional

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/home/dmccarty/.hermes"))
PROJECT_DIR = HERMES_HOME / "PROJECTS" / "tmux-workers"
DB_PATH = PROJECT_DIR / "db" / "state.db"
QUEUE_DIR = PROJECT_DIR / "queue"
WORKSPACES_DIR = PROJECT_DIR / "workspaces"
LAUNCHER_PORT = 9876

# ── REST client (launcher must be running) ──────────────────────────

def _api(path: str, method="GET", body=None):
    import urllib.request, urllib.error
    url = f"http://localhost:{LAUNCHER_PORT}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError:
        return None

# ── Tools ───────────────────────────────────────────────────────────

def spawn(title: str, task_body: str, model: Optional[str] = None,
          project: Optional[str] = None, story_id: Optional[str] = None,
          slug: Optional[str] = None) -> dict:
    """
    Spawn a tmux worker for a task.

    Args:
        title: Human-readable task name
        task_body: What the worker should do (bash code or instruction text)
        model: Optional model override (not yet wired — reserved for Hermes chat mode)

    Returns:
        {"ok": True, "task_id": "...", "session": "...", "status": "enqueued"}
        or {"ok": False, "error": "..."} if launcher is not running.
    """
    # Ensure launcher is running — spawn if not
    alive = _api("/api/tmux-workers")
    if alive is None:
        # Try to start launcher
        try:
            subprocess.Popen(
                ["python3", str(PROJECT_DIR / "launcher.py")],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True
            )
            time.sleep(2)
        except Exception:
            pass

    # Let the launcher generate the task_id using the naming convention
    body = {
        "title": title,
        "body": task_body,
        "project": project,
        "story_id": story_id,
        "slug": slug
    }

    result = _api("/api/tmux-workers/enqueue", method="POST", body=body)
    if result is None:
        return {"ok": False, "error": "launcher not running and could not start it"}

    task_id = result.get("task_id")
    display_name = result.get("display_name", title)

    # Wait briefly and check status
    time.sleep(1)
    status = _api("/api/tmux-workers")
    session = None
    if status:
        for t in status.get("tasks", []):
            if t["id"] == task_id:
                session = t.get("assigned_session")
                break

    return {
        "ok": True,
        "task_id": task_id,
        "display_name": display_name,
        "session": session,
        "status": "enqueued",
        "dashboard": f"http://localhost:{LAUNCHER_PORT}/dashboard"
    }


def list() -> dict:
    """
    List all active sessions, queued/in-progress tasks, and recent history.

    Returns:
        {"ok": True, "sessions": [...], "tasks": [...], "history": [...]}
        or {"ok": False, "error": "..."} if launcher is not running.
    """
    result = _api("/api/tmux-workers")
    if result is None:
        return {"ok": False, "error": "launcher not running"}
    result["ok"] = True
    return result


def kill(session_id: str) -> dict:
    """
    Kill a running tmux worker session.

    Args:
        session_id: e.g. "worker-task-1234567890-670064"

    Returns:
        {"ok": True} or {"ok": False, "error": "..."}
    """
    r = subprocess.run(["tmux", "kill-session", "-t", session_id],
                       capture_output=True)
    if r.returncode == 0:
        return {"ok": True}
    return {"ok": False, "error": r.stderr.decode().strip() or "session not found"}


def revision(session_id: str, feedback: str) -> dict:
    """
    Send revision feedback to a running worker — worker picks it up on next iteration.
    Writes feedback to workspace/revision.txt; bootstrap script checks for it.

    Args:
        session_id: e.g. "worker-task-1234567890-670064"
        feedback: What to fix / improve

    Returns:
        {"ok": True, "revision_file": "..."} or error
    """
    # Find workspace for session
    result = _api("/api/tmux-workers")
    if result is None:
        return {"ok": False, "error": "launcher not running"}

    task_id = None
    for s in result.get("sessions", []):
        if s["id"] == session_id:
            task_id = s.get("task_id")
            break

    if not task_id:
        return {"ok": False, "error": f"session {session_id} not found"}

    workspace = WORKSPACES_DIR / task_id
    revision_file = workspace / "revision.txt"
    revision_file.write_text(feedback)

    return {
        "ok": True,
        "revision_file": str(revision_file),
        "note": "worker will pick this up on its next iteration cycle"
    }


def status(task_id: Optional[str] = None) -> dict:
    """
    Get status of all tasks, or a specific one.

    Args:
        task_id: Optional specific task to check

    Returns:
        Task/dashboard status dict.
    """
    result = _api("/api/tmux-workers")
    if result is None:
        return {"ok": False, "error": "launcher not running"}

    if task_id:
        for t in result.get("tasks", []):
            if t["id"] == task_id:
                return {"ok": True, "task": t, "dashboard": f"http://localhost:{LAUNCHER_PORT}/dashboard"}
        return {"ok": False, "error": f"task {task_id} not found"}

    result["ok"] = True
    result["dashboard"] = f"http://localhost:{LAUNCHER_PORT}/dashboard"
    return result


def attach(session_id: str) -> dict:
    """
    Print instructions for attaching to a tmux worker session.
    Does NOT actually attach (not possible from non-interactive tool).
    Returns the tmux command to run manually.

    Args:
        session_id: e.g. "worker-task-1234567890-670064"

    Returns:
        {"ok": True, "command": "tmux attach -t <session_id>", "session": "..."}
    """
    return {
        "ok": True,
        "session": session_id,
        "command": f"tmux attach -t {session_id}",
        "detach": "Ctrl+B, D",
        "note": "Run this in a terminal — attach is interactive and cannot be automated"
    }