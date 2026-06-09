# Tmux Workers

> **Resilient tmux-based worker system** for spawning autonomous LLM agents with heartbeat monitoring, crash recovery, dispatcher-based task routing, and a goal-driven execution loop.

A persistent, inspectable, mid-flight-revisable alternative to `subprocess.run` or one-shot subagent calls. Workers run inside tmux sessions that survive gateway restarts, expose a REST API + dashboard, and enforce quality gates (tests, linting, secrets scan, code quality, git commit) before reporting completion via webhook.

## Features

- **Persistent workers** — tmux sessions outlive the parent process; reattach and inspect anytime
- **Heartbeat + crash recovery** — dispatcher detects dead sessions and reclaims orphans
- **Concurrency cap** — `MAX_CONCURRENT_WORKERS=3` by default; queue overflow returns `HTTP 429`
- **Quality gates** — every worker must pass tests, linting, secrets scan, code QC, and a git commit before reporting done
- **Goal loop (Ralph-style)** — worker iterates toward a goal text, judged by an external LLM, with continuation prompts injecting judge feedback
- **MCP server** — `tmux_workers_mcp.py` exposes `spawn`, `list`, `kill`, `status`, `revision`, `attach_info` to any Hermes agent
- **Dashboard** — web UI at `http://localhost:9876/dashboard` shows live sessions, task history, gate results
- **Audit trail** — every enqueue / dispatch / completion event written to SQLite
- **Workspace TTL** — auto-purge workspaces older than 7 days
- **Idempotent naming** — `tw_<slug>_<nnn>` task IDs, never truncated (avoids session collision)

## Quick Start

```bash
# 1. Start the launcher (dispatcher + REST API + webhook on :9876)
python3 launcher.py

# 2. Enqueue a one-shot bash task
python3 cli.py enqueue "echo hello" "echo hello world"

# 3. Enqueue an LLM-driven NL task (under 20 lines to avoid bash misclassification!)
python3 cli.py enqueue "Fix typo in README" "You are fixing a typo. Read README.md, change 'teh' to 'the', commit the fix."

# 4. Enqueue with a goal loop (worker iterates until goal met or max-turns hit)
python3 cli.py enqueue "Add tests" "Add pytest tests for cli.py" --goal "cli.py has >80% test coverage"

# 5. Or use the REST API directly
curl -X POST http://localhost:9876/api/tmux-workers/enqueue \
  -H "Content-Type: application/json" \
  -d '{"title":"My task","body":"echo hi","story_id":"F-1","slug":"my-task"}'

# 6. Monitor
curl -s http://localhost:9876/health
# {"status":"ok","workers_active":2,"workers_max":3,"queue_depth":1,"tasks_done":49,"tasks_failed":9}
```

Open the dashboard at `http://localhost:9876/dashboard`.

## Architecture

```
┌──────────────┐     POST /api/tmux-workers/enqueue
│  Hermes /    │ ──────────────────────────────────┐
│  CLI / MCP   │                                   ▼
└──────────────┘     ┌──────────────────────────────────────┐
                     │  launcher.py (port 9876)            │
                     │  ├─ REST API     (enqueue, status)  │
                     │  ├─ Webhook      (completion POST)  │
                     │  └─ Dispatcher   (every 5s tick)    │
                     └────┬──────────────┬────────────┬────┘
                          │              │            │
                          ▼              ▼            ▼
                    SQLite (db/)   queue/*.json   tmux new-session
                    sessions,      task queue     tw_<slug>_<nnn>
                    tasks,                           │
                    history                          ▼
                                              workers/bootstrap.sh
                                                    │
                                                    ├─ NL mode (LLM-driven)
                                                    │   └─ hermes chat
                                                    │      with WORKER_SOUL.md
                                                    ├─ Bash mode
                                                    │   └─ bash -c <body>
                                                    └─ Goal loop (optional)
                                                        judge → CONTINUE/DONE
                                                    │
                                                    ▼
                                              5 quality gates
                                              (tests, lint, secrets,
                                               code QC, commit)
                                                    │
                                                    ▼
                                              POST /webhook/completion
                                              → exit 0/1
```

See [`PRD.md`](./PRD.md) for the full architecture, design decisions, and history of the system.

## Project Structure

```
tmux-workers/
├── launcher.py              # Dispatcher + REST API + webhook (port 9876)
├── tmux_workers_mcp.py      # MCP server (stdio) — exposes tools to Hermes
├── tmux_workers_tools.py    # Python library — spawn/list/kill/revision/attach
├── cli.py                   # CLI: enqueue, list, kill, status, attach, revision
├── dispatcher/
│   └── dispatcherd.py       # Optional standalone daemon mode
├── webhook/
│   └── webhook_receiver.py  # Standalone webhook receiver (when not using launcher)
├── workers/
│   └── bootstrap.sh         # Task bootstrap — NL mode, bash mode, goal loop, gates
├── dashboard/
│   └── index.html           # Web UI: live sessions, tasks, history, gate results
├── db/                      # SQLite state (gitignored — regenerated on first run)
├── queue/                   # Task queue files (gitignored — runtime)
├── workspaces/              # Per-task working dirs (gitignored — TTL 7d)
├── logs/                    # Launcher + worker logs (gitignored, 5MB rotation)
├── PRD.md                   # Architecture doc
├── WORKER_SOUL.md           # Worker constitution (single-purpose)
├── GENERIC_worker_SOUL.md   # Generic dev+QA constitution (used by NL mode)
└── memory.md                # Operational notes
```

## Configuration

All config is in `launcher.py` (top-level constants). Override at runtime with env vars or by editing and restarting.

| Setting | Default | Purpose |
|---|---|---|
| `MAX_CONCURRENT_WORKERS` | `3` | Concurrency cap (overflow → HTTP 429) |
| `MAX_TASK_DURATION_MINUTES` | `60` | Hard per-task limit; `0` = no limit |
| `WORKSPACE_TTL_DAYS` | `7` | Auto-purge old workspaces; `0` = no purge |
| `HEARTBEAT_THRESHOLD_MINUTES` | `10` | Stale session detection |
| `ORPHAN_RECLAIM_ON_START` | `True` | Re-claim tmux sessions from prior launchers on startup |
| `SECRET_PATTERNS` | (5 regex) | Secrets detection — failed gates log WARNING |
| `--port` | `9876` | REST API / webhook / dashboard port |

## REST API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/tmux-workers/enqueue` | Enqueue a new task |
| `GET` | `/health` | Health summary (active workers, queue depth, done/failed counts) |
| `GET` | `/api/tmux-workers` | Full state (sessions, tasks, history) |
| `GET` | `/api/tmux-workers/sessions/<id>` | Session info + tmux attach command |
| `POST` | `/api/tmux-workers/sessions/<id>/kill` | Kill a tmux session |
| `POST` | `/webhook/completion` | Worker posts result.json here on completion |
| `GET` | `/dashboard` | Web dashboard HTML |

### Enqueue payload

```json
{
  "title": "Fix typo",           // required
  "body": "You are fixing...",   // required (NL task) or bash script
  "story_id": "F-1.2",           // optional — display name becomes "F-1.2: Fix typo"
  "project": "hermes-agent",     // optional — display name becomes "[hermes-agent] Fix typo"
  "slug": "fix-typo",            // optional — auto-derived from first 3 title words
  "goal": "README has no typos", // optional — enables goal loop
  "goal_max_turns": 5            // optional — goal loop safety cap (default 5)
}
```

**Field names matter:** `body` (not `task_body`), `title` (not `task_title`), `story_id` (not `story`). Wrong field names silently create tasks with empty bodies.

## CLI

```bash
# All commands assume you're in the project root
python3 cli.py <command> [args]

# Enqueue (bash body, short)
python3 cli.py enqueue "Run tests" "cd /path/to/proj && pytest"

# Enqueue (NL body, must be under 20 lines or is misclassified as bash)
python3 cli.py enqueue "Fix bug" "Read foo.py, fix the off-by-one error, run tests, commit."

# Enqueue with story_id (Kanban-style display)
python3 cli.py enqueue "S0.1 implementation" "..." --story F-1 --project hume-dashboard

# List
python3 cli.py list                # all tasks + sessions
python3 cli.py list --status done  # filter
python3 cli.py list --active       # only working

# Status
python3 cli.py status              # summary

# Attach (inspect a running worker)
python3 cli.py attach tw_my-slug-001
# or: tmux attach -t tw_my-slug-001

# Kill
python3 cli.py kill tw_my-slug-001

# Revision (send fix feedback to a worker mid-flight)
python3 cli.py revision tw_my-slug-001 "tests are failing on line 42, fix the import"
```

## MCP Wrapper

Add to your Hermes `config.yaml`:

```yaml
mcp_servers:
  tmux_workers:
    command: python3
    args:
      - /path/to/tmux-workers/tmux_workers_mcp.py
    timeout: 30
```

After restart, these tools become available to all agents:

- `mcp_tmux_workers_spawn` — enqueue + dispatch a worker
- `mcp_tmux_workers_list` — sessions, tasks, history
- `mcp_tmux_workers_kill` — kill a session
- `mcp_tmux_workers_status` — task or all-task status
- `mcp_tmux_workers_attach_info` — tmux attach command for a session

The MCP wrapper auto-starts the launcher if not running.

## When to use tmux-workers

✅ **Use when:**
- Task might take >10 min and needs to survive gateway restarts
- You want to inspect progress mid-flight (`tmux attach`)
- Multi-iteration fix cycle expected (build → QA → fix → re-verify)
- Work should be auditable in SQLite
- David is OK with the worker running unattended

❌ **Don't use when:**
- Quick one-shot task, <5 min, clear acceptance criteria → use `delegate_task` or just do it inline
- No inspection needed, no revision expected

## NL mode constraint: bodies under 20 lines

`is_bash_body()` in `workers/bootstrap.sh` auto-classifies task bodies >20 lines as bash mode regardless of content. For longer NL prompts, use the REST API directly with a `task_type: "nl"` override or break the task into smaller chunks.

## Development

```bash
# Syntax check
python3 -c "import ast; ast.parse(open('launcher.py').read())"
bash -n workers/bootstrap.sh

# Run unit tests (if any are added)
python3 -m pytest

# Local end-to-end test
python3 launcher.py &
sleep 2
python3 cli.py enqueue "smoke test" "echo hello > /tmp/tw-smoke && exit 0"
sleep 3
curl -s http://localhost:9876/health
```

## See also

- [`PRD.md`](./PRD.md) — full architecture, design decisions, history
- [`WORKER_SOUL.md`](./WORKER_SOUL.md) — worker constitution
- [`GENERIC_worker_SOUL.md`](./GENERIC_worker_SOUL.md) — generic dev+QA constitution
- [`memory.md`](./memory.md) — operational notes
