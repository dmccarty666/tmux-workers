#!/usr/bin/env python3
"""
tmux_workers_mcp — MCP server exposing tmux-workers as a tool
Run standalone: python3 tmux_workers_mcp.py
Or configure in config.yaml as an MCP server.

Tools exposed:
  spawn        — enqueue a new tmux worker task
  list         — list sessions, tasks, history
  kill         — kill a running tmux worker session
  revision     — send revision feedback to a worker
  status       — get status of tasks/sessions
  attach_info  — get tmux attach command for a session
"""

import json
import os
import sys
import time
from pathlib import Path

# ── Add project to path ────────────────────────────────────────────────
HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/home/dmccarty/.hermes"))
PROJECT_DIR = HERMES_HOME / "PROJECTS" / "tmux-workers"
sys.path.insert(0, str(PROJECT_DIR))

from tmux_workers_tools import spawn, list, kill, revision, status, attach

# ── MCP server manifest ───────────────────────────────────────────────

TOOLS = [
    {
        "name": "spawn",
        "description": "Spawn a new tmux worker to execute a task asynchronously. "
                       "The worker runs in a persistent tmux session, survives gateway restarts. "
                       "On completion it POSTs to the launcher webhook and the task is marked done.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Human-readable task name (shown in dashboard)"
                },
                "task_body": {
                    "type": "string",
                    "description": "What the worker should do — bash code or instruction text. "
                                   "For bash: write a script that creates output files. "
                                   "For AI: write natural language instructions for a Hermes chat agent."
                },
                "model": {
                    "type": "string",
                    "description": "Optional model override (reserved for Hermes chat mode, currently unused)"
                },
                "task_type": {
                    "type": "string",
                    "enum": ["nl", "bash"],
                    "description": "Optional execution-mode override. 'nl' = LLM-driven agent, "
                                   "'bash' = run body as shell script. If omitted, bootstrap.sh "
                                   "auto-detects (with a 20-line heuristic — set this explicitly "
                                   "for NL prompts longer than 20 lines)."
                }
            },
            "required": ["title", "task_body"]
        }
    },
    {
        "name": "list",
        "description": "List all active tmux worker sessions, queued/in-progress/completed tasks, "
                       "and recent history events. Returns dashboard URL.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "kill",
        "description": "Kill a running tmux worker session by session ID. "
                       "Use list to find active session IDs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID to kill (e.g. worker-task-1234567890-670064)"
                }
            },
            "required": ["session_id"]
        }
    },
    {
        "name": "revision",
        "description": "Send revision/feedback to a running worker. The worker picks up "
                       "revision.txt on its next iteration cycle and continues with the new context. "
                       "Use after inspecting a worker's output and finding issues.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID of the worker to revise"
                },
                "feedback": {
                    "type": "string",
                    "description": "What to fix or improve — the worker appends this to its context"
                }
            },
            "required": ["session_id", "feedback"]
        }
    },
    {
        "name": "status",
        "description": "Get status of all tasks, or a specific one. Returns current state, "
                       "assigned session, result summary, and dashboard URL.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Optional specific task ID to check. If omitted, returns all tasks."
                }
            }
        }
    },
    {
        "name": "attach_info",
        "description": "Get the tmux attach command for a session so you can inspect a running worker. "
                       "Returns the command to run in a terminal — attach is interactive and cannot be automated.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID (e.g. worker-task-1234567890-670064)"
                }
            },
            "required": ["session_id"]
        }
    }
]

# ── MCP protocol handlers ─────────────────────────────────────────────

def handle_initialize(params):
    return {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {}},
        "serverInfo": {
            "name": "tmux_workers",
            "version": "1.0.0",
            "description": "Resilient persistent tmux worker system — spawn workers, manage lifecycle"
        }
    }

def handle_list_tools(params):
    return {"tools": TOOLS}

def handle_call_tool(params):
    name = params.get("name")
    arguments = params.get("arguments", {})

    try:
        if name == "spawn":
            result = spawn(**arguments)
        elif name == "list":
            result = list()
        elif name == "kill":
            result = kill(**arguments)
        elif name == "revision":
            result = revision(**arguments)
        elif name == "status":
            result = status(**arguments)
        elif name == "attach_info":
            result = attach(**arguments)
        else:
            result = {"error": f"Unknown tool: {name}"}

        # MCP expects {content: [{type: "text", text: "..."}]} on success
        return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}

    except Exception as e:
        return {"content": [{"type": "text", "text": json.dumps({"error": str(e)})}]}

# ── Stdio transport ───────────────────────────────────────────────────

def read_message():
    """Read a JSON-RPC message from stdin."""
    raw = sys.stdin.readline()
    if not raw:
        sys.exit(0)
    return json.loads(raw)

def send_message(msg):
    """Write a JSON-RPC message to stdout."""
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()

def main():
    # Skip the Python script path itself
    while True:
        msg = read_message()
        method = msg.get("method", "")
        msg_id = msg.get("id")
        params = msg.get("params", {})

        if method == "initialize":
            # BUG FIX 2026-06-12: The initialize response MUST wrap the result
            # in a `result` key per JSON-RPC 2.0 spec, not flatten it. The old
            # code did {**handle_initialize(params)} which produced:
            #   {"jsonrpc":"2.0","id":N,"protocolVersion":..., "capabilities":..., "serverInfo":...}
            # which the Hermes MCP client could not parse (it tried to match
            # each union member — Request, Notification, Response, Error — and
            # all four failed because there was no `method`, no `result`, no
            # `error`, and the message had an `id` so it could not be a
            # Notification either). pydantic then dumped the dict in the error
            # message showing `id: <full result dict>` (which is why the error
            # log showed "id ends with manage lifecycle" — that was the
            # truncated serverInfo.description field, not actually the id).
            # Correct shape, matching the tools/list and tools/call handlers:
            send_message({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": handle_initialize(params),
            })
            # NOTE: We do NOT send `notifications/initialized` here. Per the MCP
            # spec, that notification is sent by the CLIENT to the server (not
            # the other direction). Sending it server->client would also fail
            # to parse (ServerNotification does not include
            # InitializedNotification).
        elif method == "tools/list":
            send_message({"jsonrpc": "2.0", "id": msg_id, "result": handle_list_tools(params)})
        elif method == "tools/call":
            result = handle_call_tool(params)
            send_message({"jsonrpc": "2.0", "id": msg_id, "result": result})
        elif method == "ping":
            send_message({"jsonrpc": "2.0", "id": msg_id, "result": {"status": "ok"}})
        # Ignore unhandled notifications

if __name__ == "__main__":
    main()