# Tmux Workers

**Resilient tmux worker system** for spawning autonomous LLM agents with heartbeat monitoring, crash recovery, and dispatcher-based task routing.

## Project Overview

- **Type**: Multi-Agent Orchestration / AI Infrastructure
- **Owner**: David McCarty

## Quick Start

```bash
# Launch dispatcher
python launcher.py

# Launch a worker
python cli.py spawn --soul GENERIC_worker_SOUL.md --workspace workspaces/my-task
```

## Architecture

- **dispatcher/** — tmux session manager, task queue, worker lifecycle
- **dashboard/** — real-time worker status dashboard
- **cli.py** — worker spawn CLI
- **launcher.py** — dispatcher launcher