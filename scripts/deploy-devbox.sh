#!/usr/bin/env bash
# ============================================================================
# deploy-devbox.sh — Provision the shared Hermes dev box from bare metal
# ============================================================================
#
# Run on a fresh Ubuntu install on the GMKtec EVO-T2 (or similar). Idempotent
# — safe to re-run. Each phase can be skipped with PHASE_SKIP or run in
# dry-run mode with --dry-run.
#
# Architecture reference:
#   ~/.hermes/skills/tmux-workers/references/multi-machine-dev-box-architecture-2026-06-09.md
#
# Usage:
#   sudo ./deploy-devbox.sh                 # full install
#   sudo ./deploy-devbox.sh --dry-run       # show what would happen
#   sudo ./deploy-devbox.sh --phase 1,3,5   # only run specific phases
#   sudo ./deploy-devbox.sh --skip-phase 7  # skip specific phase
#   sudo ./deploy-devbox.sh --user jmccarty # only set up one user
#
# Prerequisites:
#   - Ubuntu 22.04+ Server (Desktop not required, no GUI assumed)
#   - Run as root or with sudo
#   - Internet access (apt + git clone)
#   - Second disk at /dev/sdb (or override with --disk2-device)
#
# What it does:
#   1. OS hardening (apt upgrade, UFW, fail2ban, unattended-upgrades, ssh config)
#   2. Users (dmccarty, jmccarty, both with sudo, ssh-only auth)
#   3. /disk2 mount (ext4, fstab entry, per-user subdirs)
#   4. System tools (Python 3.11, git, tmux, build-essential, sqlite, etc.)
#   5. Docker + docker-compose
#   6. Hermes runtime install (per-user, bare — no Hindsight/LCM)
#   7. tmux-workers (cloned from GitHub, per-user)
#   8. Validation + summary
#
# After install:
#   - /disk2/{dmccarty,jmccarty}/PROJECTS/ for per-user projects
#   - /disk2/{dmccarty,jmccarty}/hermes-data/ for state
#   - Docker available to both users
#   - Hermes CLI works: `hermes --version`
#   - tmux-workers ready: `cd ~/PROJECTS/tmux-workers && python3 launcher.py`
#
# ============================================================================

set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────────
readonly DEV_USERS_DEFAULT=("dmccarty" "jmccarty")
readonly HERMES_REPO="https://github.com/NousResearch/hermes-agent.git"
readonly TMUX_WORKERS_REPO="https://github.com/dmccarty666/tmux-workers.git"
readonly DISK2_DEVICE_DEFAULT="/dev/sdb"
readonly DISK2_MOUNT="/disk2"
readonly DISK2_FSTAB_OPTS="defaults,nofail,x-systemd.device-timeout=10s"
readonly SSH_PORT=22
readonly LOG_FILE_DEFAULT="/var/log/deploy-devbox.log"
LOG_FILE="$LOG_FILE_DEFAULT"
readonly BACKUP_DIR="/var/backups/deploy-devbox"

# Colors (if terminal)
if [[ -t 1 ]]; then
    readonly C_RED=$'\033[0;31m'
    readonly C_GREEN=$'\033[0;32m'
    readonly C_YELLOW=$'\033[0;33m'
    readonly C_BLUE=$'\033[0;34m'
    readonly C_BOLD=$'\033[1m'
    readonly C_RESET=$'\033[0m'
else
    readonly C_RED="" C_GREEN="" C_YELLOW="" C_BLUE="" C_BOLD="" C_RESET=""
fi

# ── Globals ──────────────────────────────────────────────────────────────
DRY_RUN=0
PHASE_FILTER=""
SKIP_PHASES=()
USERS=("${DEV_USERS_DEFAULT[@]}")
DISK2_DEVICE="$DISK2_DEVICE_DEFAULT"
START_TIME=$(date +%s)

# ── Helpers ──────────────────────────────────────────────────────────────
log()    { echo -e "${C_BLUE}[$(date +%H:%M:%S)]${C_RESET} $*" | tee -a "$LOG_FILE" ; }
ok()     { echo -e "${C_GREEN}[$(date +%H:%M:%S)] ✓${C_RESET} $*" | tee -a "$LOG_FILE" ; }
warn()   { echo -e "${C_YELLOW}[$(date +%H:%M:%S)] ⚠${C_RESET} $*" | tee -a "$LOG_FILE" ; }
err()    { echo -e "${C_RED}[$(date +%H:%M:%S)] ✗${C_RESET} $*" | tee -a "$LOG_FILE" >&2 ; }
section(){ echo -e "\n${C_BOLD}${C_BLUE}=== $* ===${C_RESET}" | tee -a "$LOG_FILE" ; }

# Run a command, honoring dry-run mode
run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        echo -e "  ${C_YELLOW}[DRY-RUN]${C_RESET} $*" | tee -a "$LOG_FILE"
    else
        "$@"
    fi
}

# Idempotent mkdir
mkdir_p() {
    if [[ ! -d "$1" ]]; then
        run mkdir -p "$1"
    fi
}

# Check if a phase should run
phase_active() {
    local phase_num="$1"
    if [[ -n "$PHASE_FILTER" ]]; then
        [[ ",$PHASE_FILTER," == *",$phase_num,"* ]]
    else
        for skip in "${SKIP_PHASES[@]}"; do
            [[ "$skip" == "$phase_num" ]] && return 1
        done
        return 0
    fi
}

# ── Usage ────────────────────────────────────────────────────────────────
usage() {
    cat <<EOF
Usage: sudo $0 [options]

Options:
  --dry-run              Show what would happen, don't make changes
  --phase N,M,...        Only run specific phases (comma-separated)
  --skip-phase N,M,...   Skip specific phases
  --user USERNAME        Only set up this user (can repeat, default: dmccarty,jmccarty)
  --disk2-device PATH    Override /dev/sdb (e.g., /dev/nvme1n1)
  --help                 Show this help

Phases:
  1. Pre-flight checks
  2. OS hardening
  3. User creation
  4. /disk2 mount
  5. System tools
  6. Docker + compose
  7. Hermes runtime (per-user)
  8. tmux-workers (per-user, clones from GitHub)
  9. Validation + summary

Examples:
  $0 --dry-run                          # preview everything
  $0 --phase 1,2,3                      # preflight + harden + users only
  $0 --skip-phase 4                     # skip /disk2 (already mounted)
  $0 --user dmccarty                    # set up only dmccarty
EOF
}

# ── Parse args ───────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)         DRY_RUN=1; shift ;;
        --phase)           PHASE_FILTER="$2"; shift 2 ;;
        --skip-phase)      IFS=',' read -ra SKIP_PHASES <<< "$2"; shift 2 ;;
        --user)            USERS=("$2"); shift 2 ;;
        --disk2-device)    DISK2_DEVICE="$2"; shift 2 ;;
        --help|-h)         usage; exit 0 ;;
        *)                 err "Unknown option: $1"; usage; exit 1 ;;
    esac
done

# ── Pre-flight ───────────────────────────────────────────────────────────
phase_preflight() {
    section "Phase 1: Pre-flight checks"

    # Must be root (skip in dry-run so the rest can be previewed)
    if [[ $EUID -ne 0 ]]; then
        if [[ $DRY_RUN -eq 1 ]]; then
            warn "Not running as root — would fail in real run. Continuing in dry-run for preview only."
        else
            err "This script must be run as root (or with sudo)"
            exit 1
        fi
    else
        ok "Running as root"
    fi

    # Must be Ubuntu
    if [[ ! -f /etc/os-release ]]; then
        err "/etc/os-release not found. Is this Ubuntu?"
        exit 1
    fi
    . /etc/os-release
    if [[ "$ID" != "ubuntu" ]]; then
        err "This script is Ubuntu-specific (detected: $ID)"
        exit 1
    fi
    if [[ "${VERSION_ID%%.*}" -lt 22 ]]; then
        err "Ubuntu 22.04+ required (detected: $VERSION_ID)"
        exit 1
    fi
    ok "Ubuntu $VERSION_ID detected"

    # Internet check
    if ! curl -s --max-time 5 -o /dev/null https://github.com; then
        err "No internet access (github.com unreachable)"
        exit 1
    fi
    ok "Internet access verified"

    # Log file (fall back to local path if /var/log not writable)
    if [[ -w "$(dirname "$LOG_FILE")" ]] 2>/dev/null; then
        : # Use default /var/log
    else
        # Override LOG_FILE to local path for non-root or read-only systems
        if [[ -n "${DEPLOY_LOG_FILE:-}" ]]; then
            LOG_FILE="$DEPLOY_LOG_FILE"
        else
            LOG_FILE="$HOME/deploy-devbox.log"
        fi
    fi
    mkdir_p "$(dirname "$LOG_FILE")"
    touch "$LOG_FILE"
    ok "Logging to $LOG_FILE"

    # Backup dir
    mkdir_p "$BACKUP_DIR"
    ok "Backups will go to $BACKUP_DIR"

    # Show config
    log "Configuration:"
    log "  Users:        ${USERS[*]}"
    log "  /disk2 dev:   $DISK2_DEVICE"
    log "  Dry-run:      $DRY_RUN"
    log "  Phases:       ${PHASE_FILTER:-all}${SKIP_PHASES:+, skip ${SKIP_PHASES[*]}}"
}

# ── OS hardening ────────────────────────────────────────────────────────
phase_harden() {
    section "Phase 2: OS hardening"

    export DEBIAN_FRONTEND=noninteractive

    log "Updating apt cache..."
    run apt-get update -qq

    log "Upgrading installed packages (this may take a few minutes)..."
    run apt-get upgrade -y -qq -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold"

    log "Installing security packages..."
    run apt-get install -y -qq \
        ufw fail2ban unattended-upgrades \
        chrony rsync curl wget jq \
        ca-certificates gnupg lsb-release

    # UFW — only allow SSH from LAN (assume 192.168.0.0/16 + 10.0.0.0/8 private ranges)
    log "Configuring UFW (allow SSH from private networks)..."
    if [[ $DRY_RUN -eq 0 ]]; then
        # Reset to default
        ufw --force reset
        ufw default deny incoming
        ufw default allow outgoing
        # Allow SSH from private networks
        ufw allow from 192.168.0.0/16 to any port $SSH_PORT proto tcp
        ufw allow from 10.0.0.0/8 to any port $SSH_PORT proto tcp
        ufw allow from 172.16.0.0/12 to any port $SSH_PORT proto tcp
        ufw --force enable
    fi
    ok "UFW configured (SSH from private networks only)"

    # fail2ban — default SSH jail
    log "Configuring fail2ban..."
    if [[ $DRY_RUN -eq 0 ]]; then
        cat > /etc/fail2ban/jail.local <<'EOF'
[DEFAULT]
backend = systemd
bantime = 1h
findtime = 10m
maxretry = 5

[sshd]
enabled = true
port = ssh
filter = sshd
logpath = /var/log/auth.log
EOF
        systemctl enable fail2ban
        systemctl restart fail2ban
    fi
    ok "fail2ban configured (5 strikes → 1h ban)"

    # Unattended upgrades — security only
    log "Configuring unattended-upgrades (security only)..."
    if [[ $DRY_RUN -eq 0 ]]; then
        cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
Unattended-Upgrade::Allowed-Origins {
    "${distro_id}:${distro_codename}-security";
};
Unattended-Upgrade::AutoFixInterruptedDpkg "true";
Unattended-Upgrade::MinimalSteps "true";
EOF
    fi
    ok "unattended-upgrades configured (security patches auto-installed)"

    # SSH hardening — disable root login, require key auth
    log "Hardening SSH config..."
    if [[ $DRY_RUN -eq 0 ]]; then
        # Backup existing config (only if not already backed up today)
        local bname
        bname="sshd_config.bak.$(date +%Y%m%d)"
        if [[ ! -f "/etc/ssh/$bname" ]]; then
            cp /etc/ssh/sshd_config "/etc/ssh/$bname" 2>/dev/null || true
        fi
        cat > /etc/ssh/sshd_config.d/99-hermes-hardening.conf <<'EOF'
PermitRootLogin no
PasswordAuthentication no
PubkeyAuthentication yes
ChallengeResponseAuthentication no
X11Forwarding no
ClientAliveInterval 300
ClientAliveCountMax 2
MaxAuthTries 3
LoginGraceTime 30
EOF
        # Validate before reload
        if sshd -t; then
            systemctl reload sshd
            ok "SSH config validated and reloaded"
        else
            err "sshd config invalid — leaving default"
            rm /etc/ssh/sshd_config.d/99-hermes-hardening.conf
        fi
    fi
    ok "SSH hardened (no root, no password, key-only)"
}

# ── User creation ───────────────────────────────────────────────────────
phase_users() {
    section "Phase 3: User creation"

    for user in "${USERS[@]}"; do
        log "Setting up user: $user"

        if id "$user" &>/dev/null; then
            ok "User $user already exists"
        else
            log "Creating user $user with home dir..."
            run adduser --disabled-password --gecos "Hermes dev user" "$user"
            ok "User $user created"
        fi

        # Add to sudo group
        if groups "$user" | grep -q '\bsudo\b'; then
            ok "$user already in sudo group"
        else
            log "Adding $user to sudo group..."
            run usermod -aG sudo "$user"
            ok "$user added to sudo"
        fi

        # Ensure ~/.ssh exists with correct perms
        local user_home="/home/$user"
        if [[ $DRY_RUN -eq 0 ]]; then
            mkdir_p "$user_home/.ssh"
            chmod 700 "$user_home/.ssh"
            touch "$user_home/.ssh/authorized_keys"
            chmod 600 "$user_home/.ssh/authorized_keys"
            chown -R "$user:$user" "$user_home/.ssh"
        fi
        ok "$user SSH dir ready (add keys to $user_home/.ssh/authorized_keys)"
    done

    # Note: SSH public key distribution is manual — admin must add keys
    # from AImaster.local (/home/dmccarty/.ssh/id_*.pub) and AIJESSI.local
    # (/home/jmccarty/.ssh/id_*.pub) to each user's authorized_keys on this box
    warn "NEXT STEP: copy SSH public keys from AImaster.local and AIJESSI.local"
    warn "  Example: ssh-copy-id -i ~/.ssh/id_ed25519.pub dmccarty@dev.local"
    warn "  Verify:  ssh dmccarty@dev.local 'whoami'  (should NOT prompt for password)"
}

# ── /disk2 mount ────────────────────────────────────────────────────────
phase_disk2() {
    section "Phase 4: /disk2 mount"

    # Already mounted?
    if mount | grep -q " on $DISK2_MOUNT "; then
        ok "$DISK2_MOUNT already mounted"
        return
    fi

    # Device exists?
    if [[ ! -b "$DISK2_DEVICE" ]] && [[ ! -b "${DISK2_DEVICE}1" ]] && [[ ! -b "${DISK2_DEVICE}p1" ]]; then
        warn "$DISK2_DEVICE (and p1) not found — skipping disk setup"
        warn "If your disk is at a different path, use --disk2-device on next run"
        return
    fi

    # Check for existing partition
    local part="${DISK2_DEVICE}1"
    if [[ ! -b "$part" ]]; then
        part="${DISK2_DEVICE}p1"  # NVMe style
    fi
    if [[ ! -b "$part" ]]; then
        warn "No partition found on $DISK2_DEVICE — run 'parted $DISK2_DEVICE mklabel gpt && mkpart primary ext4 0% 100%' first"
        return
    fi

    # Check filesystem
    local fstype
    fstype=$(blkid -s TYPE -o value "$part" 2>/dev/null || echo "")
    if [[ -z "$fstype" ]]; then
        log "No filesystem on $part — formatting as ext4 (this destroys data!)"
        if [[ $DRY_RUN -eq 0 ]]; then
            read -rp "  Type YES to confirm formatting $part: " confirm
            [[ "$confirm" == "YES" ]] || { err "Aborted by user"; return 1; }
            mkfs.ext4 -L hermes-data "$part"
        fi
    else
        ok "Existing $fstype filesystem on $part"
    fi

    # Get UUID
    local uuid
    uuid=$(blkid -s UUID -o value "$part")

    # Mount
    log "Mounting $part at $DISK2_MOUNT..."
    mkdir_p "$DISK2_MOUNT"
    if [[ $DRY_RUN -eq 0 ]]; then
        mount "$part" "$DISK2_MOUNT"
        chmod 755 "$DISK2_MOUNT"
        chown root:root "$DISK2_MOUNT"
    fi

    # fstab
    if ! grep -q "UUID=$uuid" /etc/fstab; then
        log "Adding fstab entry..."
        if [[ $DRY_RUN -eq 0 ]]; then
            echo "UUID=$uuid $DISK2_MOUNT ext4 $DISK2_FSTAB_OPTS 0 2" >> /etc/fstab
        fi
    else
        ok "fstab already has $DISK2_MOUNT"
    fi

    ok "$DISK2_MOUNT mounted (UUID=$uuid)"

    # Create per-user subdirs
    for user in "${USERS[@]}"; do
        log "Creating /disk2/$user subdirs..."
        for subdir in PROJECTS hermes-data workspaces; do
            mkdir_p "$DISK2_MOUNT/$user/$subdir"
        done
        if [[ $DRY_RUN -eq 0 ]]; then
            chown -R "$user:$user" "$DISK2_MOUNT/$user"
            chmod 750 "$DISK2_MOUNT/$user"
        fi
        ok "/disk2/$user/ ready (PROJECTS, hermes-data, workspaces)"
    done
}

# ── System tools ────────────────────────────────────────────────────────
phase_tools() {
    section "Phase 5: System tools"

    log "Installing system packages..."
    run apt-get install -y -qq \
        python3 python3-pip python3-venv python3-dev \
        git tmux build-essential libssl-dev libffi-dev \
        sqlite3 libsqlite3-dev \
        htop iotop nethogs ncdu tree ripgrep fd-find \
        jq yq

    # Verify Python
    if [[ $DRY_RUN -eq 0 ]]; then
        local pyver py_major py_minor
        pyver=$(python3 --version | cut -d' ' -f2)
        py_major="${pyver%%.*}"
        py_minor="${pyver#*.}"; py_minor="${py_minor%%.*}"
        if [[ "$py_major" -lt 3 ]] || { [[ "$py_major" -eq 3 ]] && [[ "$py_minor" -lt 11 ]]; }; then
            warn "Python $pyver detected — Hermes prefers 3.11+"
        else
            ok "Python $pyver OK"
        fi
    fi
}

# ── Docker + compose ───────────────────────────────────────────────────
phase_docker() {
    section "Phase 6: Docker + docker-compose"

    if command -v docker &>/dev/null; then
        ok "Docker already installed: $(docker --version 2>/dev/null || echo 'unknown')"
    else
        log "Installing Docker CE..."
        if [[ $DRY_RUN -eq 0 ]]; then
            # Add Docker repo
            install -m 0755 -d /etc/apt/keyrings
            local docker_gpg
            docker_gpg=$(curl -fsSL https://download.docker.com/linux/ubuntu/gpg)
            gpg --dearmor -o /etc/apt/keyrings/docker.gpg <<< "$docker_gpg"
            chmod a+r /etc/apt/keyrings/docker.gpg
            echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
            apt-get update -qq
            apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
            ok "Docker installed"
        fi
    fi

    # Add users to docker group
    for user in "${USERS[@]}"; do
        if groups "$user" | grep -q '\bdocker\b'; then
            ok "$user already in docker group"
        else
            log "Adding $user to docker group..."
            run usermod -aG docker "$user"
            ok "$user added to docker"
        fi
    done

    # Test docker
    if [[ $DRY_RUN -eq 0 ]]; then
        if docker run --rm hello-world &>/dev/null; then
            ok "Docker hello-world OK"
        else
            warn "Docker hello-world failed — may need to log out and back in for group membership"
        fi
    fi
}

# ── Hermes runtime install ─────────────────────────────────────────────
phase_hermes() {
    section "Phase 7: Hermes runtime install (per-user)"

    for user in "${USERS[@]}"; do
        log "Installing Hermes for $user..."

        local user_home="/home/$user"
        local hermes_dir="$user_home/.hermes/hermes-agent"

        if [[ -d "$hermes_dir/.git" ]]; then
            ok "Hermes already cloned at $hermes_dir"
        else
            log "Cloning Hermes repo..."
            if [[ $DRY_RUN -eq 0 ]]; then
                mkdir_p "$user_home/.hermes"
                run sudo -u "$user" git clone "$HERMES_REPO" "$hermes_dir"
            fi
        fi

        # Set up venv
        if [[ -d "$hermes_dir/.venv" ]] || [[ -d "$hermes_dir/venv" ]]; then
            ok "Hermes venv already exists"
        else
            log "Creating Python venv for Hermes..."
            if [[ $DRY_RUN -eq 0 ]]; then
                run sudo -u "$user" python3 -m venv "$hermes_dir/.venv"
            fi
        fi

        # Install requirements
        log "Installing Hermes Python deps..."
        if [[ $DRY_RUN -eq 0 ]]; then
            local venv_python="$hermes_dir/.venv/bin/python"
            [[ ! -f "$venv_python" ]] && venv_python="$hermes_dir/venv/bin/python"
            if [[ -f "$hermes_dir/requirements.txt" ]]; then
                run sudo -u "$user" "$venv_python" -m pip install --upgrade pip
                run sudo -u "$user" "$venv_python" -m pip install -r "$hermes_dir/requirements.txt"
            else
                warn "No requirements.txt found — install manually if needed"
            fi
        fi

        # Symlink hermes CLI to ~/.local/bin
        local hermes_bin="$hermes_dir/.venv/bin/hermes"
        [[ ! -f "$hermes_bin" ]] && hermes_bin="$hermes_dir/venv/bin/hermes"
        if [[ -f "$hermes_bin" ]]; then
            log "Symlinking hermes CLI to $user_home/.local/bin/..."
            if [[ $DRY_RUN -eq 0 ]]; then
                mkdir_p "$user_home/.local/bin"
                ln -sf "$hermes_bin" "$user_home/.local/bin/hermes"
                chown -h "$user:$user" "$user_home/.local/bin/hermes"
                # Add ~/.local/bin to PATH if not already
                if ! grep -q '\.local/bin' "$user_home/.bashrc" 2>/dev/null; then
                    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$user_home/.bashrc"
                    chown "$user:$user" "$user_home/.bashrc"
                fi
            fi
            ok "Hermes CLI symlinked for $user"
        else
            warn "hermes binary not found in venv — install may need to be done manually"
        fi

        # Symlink PROJECTS to /disk2/{user}/PROJECTS
        local user_projects="/disk2/$user/PROJECTS"
        if [[ -d "$user_projects" ]]; then
            log "Symlinking $user_home/PROJECTS → $user_projects..."
            if [[ $DRY_RUN -eq 0 ]]; then
                ln -sfn "$user_projects" "$user_home/PROJECTS"
                chown -h "$user:$user" "$user_home/PROJECTS"
            fi
            ok "$user PROJECTS symlinked"
        else
            warn "$user_projects not found — did Phase 4 (disk2) run?"
        fi

        # Test the install
        if [[ $DRY_RUN -eq 0 ]]; then
            if sudo -u "$user" "$user_home/.local/bin/hermes" --version &>/dev/null; then
                ok "Hermes CLI works for $user: $(sudo -u "$user" "$user_home/.local/bin/hermes" --version 2>&1 | head -1)"
            else
                warn "Hermes CLI test failed for $user — manual debugging needed"
            fi
        fi
    done
}

# ── tmux-workers install ───────────────────────────────────────────────
phase_tmux_workers() {
    section "Phase 8: tmux-workers (per-user, from GitHub)"

    for user in "${USERS[@]}"; do
        log "Setting up tmux-workers for $user..."

        local user_home="/home/$user"
        local tmux_dir="$user_home/PROJECTS/tmux-workers"

        if [[ -d "$tmux_dir/.git" ]]; then
            ok "tmux-workers already cloned for $user"
        else
            log "Cloning tmux-workers..."
            if [[ $DRY_RUN -eq 0 ]]; then
                mkdir_p "$user_home/PROJECTS"
                run sudo -u "$user" git clone "$TMUX_WORKERS_REPO" "$tmux_dir"
            fi
        fi

        # Sanity check
        if [[ $DRY_RUN -eq 0 ]] && [[ -f "$tmux_dir/launcher.py" ]]; then
            ok "tmux-workers ready for $user at $tmux_dir"
        fi

        # Install tmux-worker shim — bootstrap.sh hardcodes `tmux-worker chat`
        # as the worker entry point. This 3-line shim delegates to
        # `hermes -p tmux-worker "$@"` so the profile's SOUL is loaded.
        # Without it, every NL worker fails with "tmux-worker: command not found".
        local shim="$user_home/.local/bin/tmux-worker"
        if [[ -x "$shim" ]]; then
            ok "tmux-worker shim already installed for $user"
        else
            log "Installing tmux-worker shim for $user..."
            if [[ $DRY_RUN -eq 0 ]]; then
                mkdir_p "$user_home/.local/bin"
                run sudo -u "$user" bash -c "printf '#!/bin/sh\nexec hermes -p tmux-worker \"\$@\"\n' > '$shim' && chmod 755 '$shim'"
            fi
        fi
    done
}

# ── Validation + summary ───────────────────────────────────────────────
phase_validate() {
    section "Phase 9: Validation + summary"

    local errors=0

    log "Checking users..."
    for user in "${USERS[@]}"; do
        if id "$user" &>/dev/null; then
            ok "User $user exists"
        else
            err "User $user missing"
            errors=$((errors + 1))
        fi
    done

    log "Checking /disk2 mount..."
    if mount | grep -q " on $DISK2_MOUNT "; then
        ok "$DISK2_MOUNT mounted"
    else
        warn "$DISK2_MOUNT not mounted — Phase 4 may have been skipped"
    fi

    log "Checking Docker..."
    if command -v docker &>/dev/null; then
        ok "Docker installed"
    else
        err "Docker missing"
        errors=$((errors + 1))
    fi

    log "Checking Hermes..."
    for user in "${USERS[@]}"; do
        if [[ -x "/home/$user/.local/bin/hermes" ]]; then
            ok "Hermes CLI installed for $user"
        else
            warn "Hermes CLI not found for $user at expected path"
        fi
    done

    log "Checking tmux-workers..."
    for user in "${USERS[@]}"; do
        if [[ -f "/home/$user/PROJECTS/tmux-workers/launcher.py" ]]; then
            ok "tmux-workers cloned for $user"
        else
            warn "tmux-workers not found for $user"
        fi
    done

    local elapsed=$(( $(date +%s) - START_TIME ))
    local mins=$(( elapsed / 60 ))
    local secs=$(( elapsed % 60 ))

    section "Summary"
    log "Elapsed: ${mins}m ${secs}s"
    log "Log:     $LOG_FILE"
    log "Users:   ${USERS[*]}"
    log "Errors:  $errors"

    if [[ $errors -gt 0 ]]; then
        err "$errors errors detected — see log"
        return 1
    fi

    cat <<EOF

${C_GREEN}${C_BOLD}╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║   Dev box ready! Next steps:                                ║
║                                                              ║
║   1. Copy SSH keys from AImaster.local and AIJESSI.local    ║
║      ssh-copy-id -i ~/.ssh/id_ed25519.pub dmccarty@dev.local║
║      ssh-copy-id -i ~/.ssh/id_ed25519.pub jmccarty@dev.local║
║                                                              ║
║   2. Test login:                                            ║
║      ssh dmccarty@dev.local 'whoami; hermes --version'      ║
║                                                              ║
║   3. Run shared Postgres (separate script):                 ║
║      See deploy-postgres.sh (next deliverable)              ║
║                                                              ║
║   4. Configure Hermes for shared use:                       ║
║      Each user adds Hindsight URL to ~/.hermes/.env         ║
║      HINT_BACKEND_URL=http://AImaster.local:8787            ║
║                                                              ║
║   5. Start tmux-workers (per-user, on the dev box):         ║
║      cd ~/PROJECTS/tmux-workers                              ║
║      # Add bearer token + bind 0.0.0.0 first                ║
║      python3 launcher.py                                     ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝${C_RESET}

EOF
}

# ── Main ────────────────────────────────────────────────────────────────
main() {
    # Resolve log file path early (before any log() call can fail)
    if [[ ! -w "$(dirname "$LOG_FILE")" ]] 2>/dev/null; then
        if [[ -n "${DEPLOY_LOG_FILE:-}" ]]; then
            LOG_FILE="$DEPLOY_LOG_FILE"
        elif [[ -n "${HOME:-}" ]] && [[ -d "$HOME" ]]; then
            LOG_FILE="$HOME/deploy-devbox.log"
        else
            LOG_FILE="/tmp/deploy-devbox.log"
        fi
        mkdir_p "$(dirname "$LOG_FILE")"
        touch "$LOG_FILE"
    fi

    log "════════════════════════════════════════════════════"
    log "deploy-devbox.sh — $(date)"
    log "════════════════════════════════════════════════════"

    if [[ $DRY_RUN -eq 1 ]]; then
        warn "DRY-RUN MODE — no changes will be made"
    fi

    # Run each phase only if it's active per the filter
    phase_active 1 && phase_preflight || true
    phase_active 2 && phase_harden || true
    phase_active 3 && phase_users || true
    phase_active 4 && phase_disk2 || true
    phase_active 5 && phase_tools || true
    phase_active 6 && phase_docker || true
    phase_active 7 && phase_hermes || true
    phase_active 8 && phase_tmux_workers || true
    phase_active 9 && phase_validate || true
}

main "$@"
