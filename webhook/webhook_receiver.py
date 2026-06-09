#!/usr/bin/env python3
"""
Tmux Worker Webhook Receiver
Lightweight HTTP server that receives completion POSTs from workers.
Writes completion payload to a queue file; dispatcher reads it on next tick.

Usage: python3 webhook_receiver.py [--port 8765]
"""
import argparse
import json
import os
import signal
import sys
from pathlib import Path
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/home/dmccarty/.hermes"))
PROJECT_DIR = HERMES_HOME / "PROJECTS" / "tmux-workers"
COMPLETION_QUEUE = PROJECT_DIR / "queue" / "completions"
DB_PATH = PROJECT_DIR / "db" / "state.db"

# Make sure completion queue exists
COMPLETION_QUEUE.mkdir(parents=True, exist_ok=True)


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/webhook/completion":
            self.send_error(404, "Not found")
            return
        
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return
        
        # Add receipt timestamp
        payload["received_at"] = datetime.now().isoformat()
        
        # Write to completion queue (dispatcher picks these up)
        completion_file = COMPLETION_QUEUE / f"{payload.get('task_id', 'unknown')}.json"
        completion_file.write_text(json.dumps(payload, indent=2))
        
        # Also update SQLite directly for dashboard queries
        try:
            import sqlite3
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            task_id = payload.get("task_id")
            session = payload.get("session")
            status = payload.get("status", "done")
            
            if status == "done":
                c.execute("""UPDATE tasks SET status='done', result_summary=?, artifacts=?,
                             completed_at=CURRENT_TIMESTAMP WHERE id=?""",
                          (payload.get("summary"), json.dumps(payload.get("artifacts", [])), task_id))
                c.execute("UPDATE sessions SET status='done', result_summary=? WHERE id=?",
                          (payload.get("summary"), session))
                c.execute("INSERT INTO history (task_id, event, detail) VALUES (?, 'completed', ?)",
                          (task_id, json.dumps(payload)))
            elif status == "blocked":
                c.execute("UPDATE sessions SET status='blocked' WHERE id=?", (session,))
                c.execute("INSERT INTO history (task_id, event, detail) VALUES (?, 'blocked', ?)",
                          (task_id, json.dumps(payload)))
            # Unknown status — log it but don't update
            
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[webhook] SQLite update failed: {e}")
        
        print(f"[webhook] received completion for {payload.get('task_id')} status={status}")
        
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"received": True}).encode())
    
    def log_message(self, format, *args):
        # Silence default logging, we use print with prefixes
        pass


def run(port):
    server_address = ("", port)
    httpd = HTTPServer(server_address, WebhookHandler)
    print(f"[webhook] listening on port {port}")
    httpd.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tmux worker webhook receiver")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on (default: 8765)")
    args = parser.parse_args()
    
    # Handle signals gracefully
    def handle_signal(sig, frame):
        print("\n[webhook] shutting down...")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    
    run(args.port)