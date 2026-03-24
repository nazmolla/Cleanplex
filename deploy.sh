#!/usr/bin/env bash
# deploy.sh — Deploy Cleanplex to a remote Linux host via SSH.
#
# Usage:
#   ./deploy.sh <user@host> [remote_dir]
#
# Arguments:
#   user@host   (required) e.g. myuser@192.168.1.10
#   remote_dir  (optional) defaults to /opt/cleanplex
#
# Requirements (local): ssh, rsync
# Requirements (remote): sudo, apt-get or dnf  (Python/Node auto-installed if missing)

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
if [ -z "${1:-}" ]; then
  echo "Usage: $0 <user@host> [remote_dir]" >&2
  exit 1
fi

TARGET="$1"
REMOTE_DIR="${2:-/opt/cleanplex}"
SERVICE_NAME="cleanplex"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REMOTE_USER="${TARGET%%@*}"
REMOTE_HOST="${TARGET##*@}"

# ── Helpers ───────────────────────────────────────────────────────────────────
info() { printf '\033[1;34m[INFO]\033[0m  %s\n' "$*"; }
ok()   { printf '\033[1;32m[OK]\033[0m    %s\n' "$*"; }
die()  { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

ssh_run() {
  ssh -o StrictHostKeyChecking=accept-new \
      -o BatchMode=yes \
      "${TARGET}" "$@"
}

# ── 1. Verify SSH ─────────────────────────────────────────────────────────────
info "Testing SSH connection to ${TARGET} ..."
ssh_run true 2>/dev/null || die "SSH failed. Ensure key-based auth is configured for ${TARGET}."
ok "SSH connection OK"

# ── 2. Prepare remote directory ───────────────────────────────────────────────
info "Preparing ${REMOTE_HOST}:${REMOTE_DIR} ..."
ssh_run "sudo mkdir -p '${REMOTE_DIR}' && sudo chown '${REMOTE_USER}:${REMOTE_USER}' '${REMOTE_DIR}'"

# ── 3. Sync source files ──────────────────────────────────────────────────────
info "Syncing source files ..."
rsync -az --delete --info=progress2 \
  --exclude='.git' \
  --exclude='frontend/node_modules' \
  --exclude='cleanplex/web/static' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='*.egg-info' \
  --exclude='.env' \
  "${SCRIPT_DIR}/" "${TARGET}:${REMOTE_DIR}/"
ok "Source synced"

# ── 4. Remote setup ───────────────────────────────────────────────────────────
info "Running remote setup (this may take a few minutes on first run) ..."

ssh_run bash -s -- "${REMOTE_DIR}" "${REMOTE_USER}" "${SERVICE_NAME}" <<'REMOTE_SCRIPT'
set -euo pipefail

REMOTE_DIR="$1"
REMOTE_USER="$2"
SERVICE_NAME="$3"

log()  { printf '[remote] %s\n' "$*"; }
die()  { printf '[remote] ERROR: %s\n' "$*" >&2; exit 1; }

# ── Detect package manager ────────────────────────────────────────────────────
if command -v apt-get &>/dev/null; then
  PKG_MGR="apt"
elif command -v dnf &>/dev/null; then
  PKG_MGR="dnf"
else
  die "No supported package manager found (apt-get / dnf required)."
fi

# ── Python 3.11+ ──────────────────────────────────────────────────────────────
find_python() {
  for cmd in python3.11 python3.12 python3.13 python3; do
    if command -v "$cmd" &>/dev/null; then
      local ok
      ok=$("$cmd" -c 'import sys; print(sys.version_info >= (3,11))' 2>/dev/null || echo False)
      [ "$ok" = "True" ] && { echo "$cmd"; return; }
    fi
  done
}

PYTHON=$(find_python)
if [ -z "$PYTHON" ]; then
  log "Python 3.11+ not found — installing ..."
  if [ "$PKG_MGR" = "apt" ]; then
    sudo apt-get update -qq
    sudo apt-get install -y python3.11 python3.11-venv python3-pip curl
  else
    sudo dnf install -y python3.11 python3-pip curl
  fi
  PYTHON=python3.11
fi
log "Python: $($PYTHON --version)"

# ── Node.js 18+ ───────────────────────────────────────────────────────────────
need_node=false
if command -v node &>/dev/null; then
  node_major=$(node -e 'process.stdout.write(process.version.slice(1).split(".")[0])')
  [ "$node_major" -lt 18 ] && need_node=true
else
  need_node=true
fi

if $need_node; then
  log "Node.js 18+ not found — installing ..."
  if [ "$PKG_MGR" = "apt" ]; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - 2>/dev/null
    sudo apt-get install -y nodejs
  else
    sudo dnf module install -y nodejs:20
  fi
fi
log "Node: $(node --version)  npm: $(npm --version)"

# ── Build frontend ────────────────────────────────────────────────────────────
log "Building frontend ..."
cd "${REMOTE_DIR}/frontend"
npm install --silent
npm run build --silent
log "Frontend built → cleanplex/web/static/"

# ── Python venv + package ─────────────────────────────────────────────────────
cd "${REMOTE_DIR}"
VENV="${REMOTE_DIR}/.venv"

if [ ! -d "$VENV" ]; then
  log "Creating Python venv at ${VENV} ..."
  $PYTHON -m venv "$VENV"
fi

log "Installing Python package ..."
"${VENV}/bin/pip" install --quiet --upgrade pip setuptools wheel
"${VENV}/bin/pip" install --quiet .
log "Package installed."

# ── Write systemd service (with venv python + correct user) ──────────────────
log "Writing systemd service ..."
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

sudo tee "$SERVICE_FILE" > /dev/null <<SERVICE
[Unit]
Description=Cleanplex — Plex content filter service
Documentation=https://github.com/nazmolla/Cleanplex
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${REMOTE_USER}
Group=${REMOTE_USER}
WorkingDirectory=${REMOTE_DIR}
ExecStart=${VENV}/bin/python -m cleanplex
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=cleanplex

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl restart "${SERVICE_NAME}"

# Give service a moment to start
sleep 2

if systemctl is-active --quiet "${SERVICE_NAME}"; then
  log "Service '${SERVICE_NAME}' is running."
else
  log "WARNING: service may not have started. Check: journalctl -u ${SERVICE_NAME} -n 50"
fi
REMOTE_SCRIPT

# ── Done ──────────────────────────────────────────────────────────────────────
ok "Deployment complete!"
echo ""
echo "  Host:    ${REMOTE_HOST}"
echo "  Service: sudo systemctl status ${SERVICE_NAME}"
echo "  Logs:    sudo journalctl -u ${SERVICE_NAME} -f"
echo "  Web UI:  http://${REMOTE_HOST}:7979"
echo ""
