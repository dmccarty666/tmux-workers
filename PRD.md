# Tmux Workers — Resilient Autonomous Agent System

## Overview

Tmux Workers is a persistent agent execution system that spawns autonomous Hermes agents in tmux sessions. Workers survive gateway restarts, support reattach for inspection/fixes, and include an optional **goal loop** (Ralph loop) with LLM judging to keep work on track.

**Status:** Production-ready. 50+ tasks executed, all features verified end-to-end.

---

## Architecture

```
┌────────────────────────────────────────────────────────────┐
│  Hermes / Dashboard / API                                  │
│  · enqueue tasks via REST API or dashboard form           │
│  · monitor sessions, attach/kill, filter by status        │
│  · receives completion webhooks                           │
└──────────┬─────────────────────────────────────────────────┘
           │ writes queue/<task>.json
           ▼
┌──────────────────────────────────────────────────────────┐
│  Launcher (launcher.py) — single process                  │
│  · dispatcher loop: watches queue, spawns tmux sessions  │
│  · heartbeat monitoring (10 min timeout)                 │
│  · max duration enforcement (60 min)                     │
│  · cleanup: workspaces, queue files, DB, stale sessions  │
│  · inline HTTP server on :9876                           │
│    ├─ GET  /api/tmux-workers         — full state        │
│    ├─ GET  /api/tmux-workers/sessions/<id> — attach info │
│    ├─ GET  /                — dashboard HTML             │
│    ├─ POST /api/tmux-workers/enqueue — spawn task        │
│    ├─ POST /api/tmux-workers/sessions/<id>/kill          │
│    └─ POST /webhook/completion      — worker callback    │
└──────────┬───────────────────────────────────────────────┘
           │ spawns
           ▼
┌──────────────────────────────────────────────────────────┐
│  tmux session (worker-<task_id>)                          │
│  · runs workers/bootstrap.sh                             │
│  · NL mode: tmux-worker profile chat (SOUL baked in)     │
│  · Bash mode: raw bash execution                         │
│  · Quality gates: tests → linting → secrets → code       │
│    quality → commit                                      │
│  · Optional Goal Loop: LLM judge evaluates work          │
│  · Result: result.json + webhook POST                    │
│  · Stays alive after completion for reattach             │
└──────────────────────────────────────────────────────────┘
           │
           │ completion POST
           ▼
┌──────────────────────────────────────────────────────────┐
│  SQLite state.db + dashboard                              │
│  · sessions, tasks, history tables                       │
│  · dashboard: dark theme, filter by status, attach/kill  │
│  · auto-refresh every 10s                                │
└──────────────────────────────────────────────────────────┘
```

---

## Directory Structure

```
~/.hermes/PROJECTS/tmux-workers/
├── launcher.py                 # single-process launcher (dispatcher + API + webhook)
├── tmux_workers_tools.py       # Hermes tools: spawn, list, kill workers
├── tmux_workers_mcp.py         # MCP server for worker management
├── cli.py                      # CLI utilities (attach, kill, enqueue)
├── PRD.md                      # this file
├── GENERIC_worker_SOUL.md      # worker SOUL (identity, gates, completion protocol)
├── WORKER_SOUL.md              # legacy SOUL (kept for reference)
├── dispatcher/
│   └── dispatcherd.py          # original dispatcher (legacy, launcher.py is primary)
├── workers/
│   └── bootstrap.sh            # tmux session bootstrap script
├── queue/                       # task JSON files + completions/
│   └── completions/            # webhook payloads
├── workspaces/                  # per-task workspaces (auto-cleaned after 7 days)
├── dashboard/
│   └── index.html              # single-page dashboard
├── db/
│   └── state.db                # SQLite state store
└── logs/
    └── launcher.log             # rotated at 5 MB, 3 backups kept
```

---

## Data Model (SQLite)

### sessions
| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PK | tmux session name (e.g. `tw_e2e-final-v5_002`) |
| task_id | TEXT | assigned task id |
| status | TEXT | `working\|done\|blocked\|dead\|killed\|archived` |
| created_at | DATETIME | |
| heartbeat_at | DATETIME | last check-in |
| result_summary | TEXT | |

### tasks
| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PK | task id (auto-generated slug) |
| title | TEXT | task title |
| body | TEXT | full task description / instructions |
| status | TEXT | `queued\|assigned\|done\|failed\|revision` |
| assigned_session | TEXT FK | tmux session assigned |
| result_summary | TEXT | |
| artifacts | TEXT | JSON array of file paths |
| created_at | DATETIME | |
| completed_at | DATETIME | |
| project | TEXT | optional project tag |
| story_id | TEXT | optional story/feature ID |
| slug | TEXT | URL-friendly slug for task id generation |
| seq | INTEGER | auto-increment per slug |
| display_name | TEXT | human-readable name |

### history
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | autoincrement |
| task_id | TEXT FK | |
| event | TEXT | `enqueued\|assigned\|completed\|failed\|blocked` |
| detail | TEXT | JSON detail |
| at | DATETIME | |

---

## Worker Lifecycle

### Spawn
```
Enqueue (API / dashboard / Hermes tool)
  → launcher writes queue/<task_id>.json with all fields:
    { title, body, project, story_id, goal, goal_max_turns }
  → dispatcher picks up task, creates workspace/
  → tmux new-session -d -s <session_id> bash bootstrap.sh <task_id> <workspace> <session_id>
  → DB: session status=working, task status=assigned
```

### Execution Modes

**NL Mode** (default): Uses the `tmux-worker` Hermes profile
- SOUL baked into profile identity (not passed in prompt)
- Model: deepseek/deepseek-v4-pro (configurable per profile)
- Tools: terminal, file, web
- Max 90 turns per invocation

**Bash Mode**: Detected when task body looks like shell commands
- Runs raw bash, captures exit code
- Same quality gates apply afterward

**Goal Loop** (optional): When `goal` field is set
- After each LLM turn, a judge model evaluates result against goal
- Judge: Google Gemini 2.5 Flash Lite (free tier, via OpenRouter)
- If goal NOT met: continuation prompt injected, LLM runs again
- Loops until DONE or `goal_max_turns` exhausted (default 5)
- Judge sends goal text + result summary + artifact contents to LLM

### Quality Gates (shared post-execution)
Run in order after every LLM/batch execution:
1. **Tests** — pytest if tests exist, SKIP otherwise
2. **Linting** — ruff or py_compile, SKIP if no linter
3. **Secrets** — grep for key patterns, PASS on no matches
4. **Code Quality** — checks existing `.gate_code_quality.json`
5. **Commit** — git commit if repo exists, SKIP otherwise

Aggregate summary written to `.gate_summary.json`, then result.json enriched.

### Completion
```
Worker writes result.json
  → POST /webhook/completion with full payload
  → launcher processes completion, updates DB
  → Session stays alive with interactive bash shell
  → Custom prompt: [task_id ✅] workspace $
  → Rettachable via: tmux attach -t <session_id>
```

### Reattach
- Dashboard: click "attach" → copies `tmux attach -t <session_id>` to clipboard
- CLI: `tmux attach -t tw_<task_id>`
- API: `GET /api/tmux-workers/sessions/<id>` returns `{exists, attach_cmd}`
- Hermes: `tmux send-keys -t <session> "command" Enter`

---

## Dashboard

Single HTML file at `http://localhost:9876/`, dark theme, auto-refreshes every 10s.

**Features:**
- **Sessions panel** — filterable (all/alive/done/dead), status badges, attach/kill buttons
- **Queue panel** — pending tasks with project/story context
- **Completed panel** — last 20 done tasks with summaries
- **History panel** — full audit log (auto-pruned after 30 days)
- **Assign New Task form** — title, project, story, task body, goal, max turns

**Goal field:** New field in the spawn form. When set, enables the Ralph loop:
- Goal text defines success criteria
- Max turns limits iterations (default 5)
- Judge LLM evaluates against artifacts

---

## Cleanup & Housekeeping

All cleanup runs in the launcher's main loop (every 5 seconds), no cron dependency:

| Resource | TTL | Action |
|----------|-----|--------|
| Workspaces | 7 days | `shutil.rmtree` |
| Done tmux sessions | 2 hours idle | Auto-killed, status → archived |
| Queue files | 7 days | Deleted |
| Completion files | 7 days | Deleted |
| DB history rows | 30 days | Pruned |
| DB file | Daily | VACUUM (reclaims space) |
| Launcher log | 5 MB | Rotated (3 backups) |

Configurable at top of `launcher.py`: `WORKSPACE_TTL_DAYS`, `MAX_LOG_BYTES`, `LOG_BACKUP_COUNT`.

---

## tmux-worker Profile

A dedicated Hermes profile at `~/.hermes/profiles/tmux-worker/`:
- **SOUL.md:** Worker identity, TDD discipline, quality gates, completion protocol
- **Model:** deepseek/deepseek-v4-pro (OpenRouter)
- **Provider:** openrouter
- **Wrapper:** `~/.local/bin/tmux-worker` → `hermes -p tmux-worker "$@"`

The SOUL is auto-loaded as the agent's system identity — no prompt-stuffing needed.

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/tmux-workers` | Full state: sessions, tasks, history |
| GET | `/api/tmux-workers/sessions/<id>` | Session info + attach command |
| GET | `/` or `/dashboard` | Dashboard HTML |
| GET | `/health` | Health check |
| POST | `/api/tmux-workers/enqueue` | Enqueue new task |
| POST | `/api/tmux-workers/sessions/<id>/kill` | Kill session + update DB |
| POST | `/webhook/completion` | Worker completion callback |

**Enqueue payload:**
```json
{
  "title": "Task title",
  "body": "Task description / instructions",
  "project": "optional-project",
  "story_id": "optional-story",
  "slug": "optional-slug",
  "goal": "Optional success criteria for goal loop",
  "goal_max_turns": 5
}
```

---

## Comparison: delegate_task vs tmux-workers vs Kanban

| | delegate_task | tmux-workers | Kanban |
|---|---|---|---|
| Persistence | Ephemeral | Survives restarts | Survives restarts |
| Reattach | No | Yes (tmux attach) | Via board |
| Goal loop | No | Yes (LLM judge) | Via orchestrator |
| Multi-phase | No | Manual | Yes (orchestrator) |
| Dashboard | No | Yes (:9876) | Yes |
| Best for | Quick one-shots | Ad-hoc, exploratory, long-running | Structured sprints |

---

## Key Design Decisions

1. **Launcher is single-process** — dispatcher + API + webhook all in one Python process for simplicity
2. **SOUL baked into profile** — not stuffed into every prompt, saves ~12KB context per task
3. **Pipeline subshell bug fixed** — task type detection moved outside `{...}|tee` to avoid bash subshell variable loss
4. **Secrets gate fixed** — grep exit code 1 = "no matches" = PASS (not fail)
5. **Judge is real LLM** — not a heuristic; evaluates goal text + artifact contents
6. **Sessions stay alive** — interactive shell after completion for reattach/inspect/fix
7. **Auto-cleanup** — everything has a TTL, no disk sprawl
