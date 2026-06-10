# deploy-devbox.sh — Provisioning script for the shared dev box

## Purpose

Provisions a fresh Ubuntu install on the GMKtec EVO-T2 (or similar) into a
ready-to-run shared development box for the Hermes agent system.

Sets up two Linux users (`dmccarty`, `jmccarty`), both with sudo, plus
`/disk2` data mount, Docker, and Hermes runtime installed per-user. Idempotent
— safe to re-run. Each phase is optional and can be skipped.

**Architecture reference:**
`~/.hermes/skills/tmux-workers/references/multi-machine-dev-box-architecture-2026-06-09.md`

## When to use

Run this once when the GMKtec arrives (or any replacement dev box). Re-runnable
for re-provisioning or to add a phase that was skipped the first time.

## Prerequisites

- **OS:** Ubuntu 22.04+ Server (24.04 recommended). Desktop not required.
- **Privileges:** Run as `root` or with `sudo`
- **Network:** Internet access (apt repos + GitHub for clones)
- **Storage:** Second disk at `/dev/sdb` (override with `--disk2-device`).
  If your disk is at a different path (e.g. `/dev/nvme1n1`), pass it via flag.

## Quick start

```bash
# 1. Preview what would happen (no changes)
sudo ./deploy-devbox.sh --dry-run

# 2. Full install (≈30-60 min depending on network)
sudo ./deploy-devbox.sh

# 3. Re-run for a specific phase (e.g. add jmccarty later)
sudo ./deploy-devbox.sh --user jmccarty --phase 3,7
```

## What each phase does

| # | Phase | Description | Time |
|---|---|---|---|
| 1 | Pre-flight | Root check, Ubuntu version, internet, log setup | 5s |
| 2 | OS hardening | apt upgrade, UFW (SSH from LAN only), fail2ban, unattended-upgrades, SSH hardening (no root, no password, key-only) | 5-15min |
| 3 | User creation | Create `dmccarty` and `jmccarty` (idempotent), add to sudo, prep `~/.ssh/` | 30s |
| 4 | /disk2 mount | Detect second disk, format as ext4 if needed, mount, fstab entry, create per-user subdirs (PROJECTS, hermes-data, workspaces) | 1-2min |
| 5 | System tools | Python 3.11+, git, tmux, build-essential, sqlite, ripgrep, fd, etc. | 2-5min |
| 6 | Docker + compose | Install Docker CE from official repo, add users to `docker` group, test with `hello-world` | 2-3min |
| 7 | Hermes runtime | Per-user: clone NousResearch/hermes-agent, create venv, pip install, symlink `~/.local/bin/hermes`, symlink `~/PROJECTS → /disk2/{user}/PROJECTS` | 3-5min |
| 8 | tmux-workers | Per-user: clone dmccarty666/tmux-workers from GitHub | 1min |
| 9 | Validation | Check users, mounts, Docker, Hermes, tmux-workers; print summary | 5s |

## Options

```
--dry-run              Show what would happen, don't make changes
--phase N,M,...        Only run specific phases (comma-separated)
--skip-phase N,M,...   Skip specific phases
--user USERNAME        Only set up this user (can repeat, default: dmccarty,jmccarty)
--disk2-device PATH    Override /dev/sdb (e.g., /dev/nvme1n1)
--help                 Show help
```

## Post-install

After the script completes successfully, do these manually:

1. **Copy SSH keys** from AImaster.local and AIJESSI.local:
   ```bash
   # From AImaster.local (David)
   ssh-copy-id -i ~/.ssh/id_ed25519.pub dmccarty@dev.local

   # From AIJESSI.local (Jessica)
   ssh-copy-id -i ~/.ssh/id_ed25519.pub jmccarty@dev.local
   ```

2. **Verify login works without password** (should NOT prompt):
   ```bash
   ssh dmccarty@dev.local 'whoami; hermes --version'
   ```

3. **Configure Hindsight URL** in each user's `~/.hermes/.env`:
   ```bash
   HINT_BACKEND_URL=http://AImaster.local:8787
   ```
   (David points to AImaster, Jessica to AIJESSI.local)

4. **Start tmux-workers** (after Phase C — shared Postgres — is done):
   ```bash
   cd ~/PROJECTS/tmux-workers
   # Edit launcher.py to add bearer token + bind 0.0.0.0
   python3 launcher.py
   ```

## What it does NOT do

- **Doesn't install shared Postgres** — that's a separate script (`deploy-postgres.sh`, future work)
- **Doesn't configure Hindsight** — that's per-user config, not infrastructure
- **Doesn't set up Kanban** — separate work
- **Doesn't install tmux-worker profile** — that's the per-user config that AImaster already has; the dev box just needs the bare runtime
- **Doesn't back up /disk2** — set up a cron separately
- **Doesn't install an orchestrator (Hermes/Fiona)** — those live on the home machines

## How to test before the real machine

On AImaster, run with `--dry-run` to preview all phases without making changes:
```bash
sudo /home/dmccarty/.hermes/PROJECTS/tmux-workers/scripts/deploy-devbox.sh --dry-run
```

For full real-world testing, set up an LXC container or VM with Ubuntu 24.04:
```bash
# Quick LXC test
lxc launch ubuntu:24.04 devbox-test
lxc exec devbox-test -- bash -c "apt update && apt install -y git"
lxc exec devbox-test -- bash -c "curl -sL https://raw.githubusercontent.com/dmccarty666/tmux-workers/main/scripts/deploy-devbox.sh -o /tmp/deploy.sh"
lxc exec devbox-test -- bash -c "chmod +x /tmp/deploy.sh && /tmp/deploy.sh --dry-run"
```

## Troubleshooting

**"No internet access"** — check `curl https://github.com` from the box. If behind a proxy, set `http_proxy` / `https_proxy` env vars before running.

**"User dmccarty missing" at end** — usually means `--user` was set to only one user. Re-run with both: `sudo ./deploy-devbox.sh --user dmccarty --user jmccarty`.

**"Docker hello-world failed"** — may need to log out and back in for `docker` group membership to take effect. Or run `newgrp docker` in the current shell.

**"Hermes CLI test failed"** — usually means the venv exists but `requirements.txt` is missing or pip install failed. Check `ls /home/$user/.hermes/hermes-agent/` and `cat $HOME/deploy-devbox.log`.

## Idempotency notes

- **Users** — `adduser` is a no-op if user exists. Re-running is safe.
- **/disk2 mount** — detected by `mount | grep`. Skipped if already mounted.
- **Docker** — installs if missing, skips if present.
- **Hermes** — clones if `.git` missing, creates venv if missing.
- **tmux-workers** — clones if `.git` missing.

The script is designed to be re-runnable as part of routine maintenance, not
just first install.
