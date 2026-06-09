# Changelog

All notable changes to tmux-workers are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Initial public release of the tmux-workers implementation
- `launcher.py` — dispatcher thread + REST API + webhook server (port 9876)
- `tmux_workers_mcp.py` — MCP server exposing spawn/list/kill/status/revision/attach_info tools
- `tmux_workers_tools.py` — Python library for programmatic worker control
- `cli.py` — CLI: enqueue, list, kill, status, attach, revision
- `dispatcher/dispatcherd.py` — optional standalone dispatcher daemon
- `webhook/webhook_receiver.py` — standalone webhook receiver
- `workers/bootstrap.sh` — task bootstrap with NL mode, bash mode, and goal loop
- `dashboard/index.html` — web dashboard for live session/task monitoring
- Quality gates: tests, linting, secrets scan, code QC, git commit
- Goal loop (Ralph-style) with external LLM judge + continuation prompts
- Heartbeat + crash recovery, orphan session reclaim on startup
- Workspace TTL cleanup (default 7 days)
- Concurrency cap (default MAX_CONCURRENT_WORKERS=3, HTTP 429 on overflow)
- Secret detection with regex patterns + gate failure
- 5MB rotating logs

### Verified (2026-06-09)
- Single-agent and 5-agent concurrent tests passed (2026-05-28)
- Recovery from git history (commit `d47e857` in dmccarty666/hermes-workspace) confirmed
- Launcher smoke test: 49 historical tasks_done + 9 tasks_failed preserved in state.db

[Unreleased]: https://github.com/dmccarty666/tmux-workers/compare/main...HEAD
