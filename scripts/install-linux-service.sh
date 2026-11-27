#!/bin/bash
# install-linux-service.sh — Install Cleanplex (Hayaa for Plex) as a systemd service.
#
# Usage:
#   sudo ./scripts/install-linux-service.sh [run_user]
#   sudo ./scripts/install-linux-service.sh --uninstall [run_user]
#
#   run_user  (optional) account to run the service as, defaults to "plex"
#
# Re-running the script updates the installed code and restarts the service.
# Python 3.12 is provisioned via uv inside the install dir, so no system Python
# is required. The prebuilt frontend in cleanplex/web/static is used.

set -euo pipefail

SERVICE_NAME="cleanplex"
INSTALL_DIR="/opt/cleanplex"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PORT="7979"
HOST="${HAYAA_HOST:-0.0.0.0}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

UNINSTALL=0
RUN_USER=""

while [ $# -gt 0 ]; do
  case "$1" in
    --uninstall)
      UNINSTALL=1
      shift
      ;;
    -h|--help)
      echo "Usage: sudo $0 [--uninstall] [run_user]"
      exit 0
      ;;
    -*)
      printf '\033[1;31m[ERROR]\033[0m Unknown option: %s\n' "$1" >&2
      exit 2
      ;;
    *)
      RUN_USER="$1"
      shift
      ;;
  esac
done

info() { printf '\033[1;34m[INFO]\033[0m  %s\n' "$*"; }
ok()   { printf '\033[1;32m[OK]\033[0m    %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m  %s\n' "$*"; }
die()  { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "Run with sudo: sudo $0 ${RUN_USER:-plex}"

if [ "$UNINSTALL" -eq 1 ]; then
  if [ -z "$RUN_USER" ] && [ -f "$UNIT_FILE" ]; then
    RUN_USER="$(sed -n 's/^User=//p' "$UNIT_FILE" | head -n 1)"
  fi
  RUN_USER="${RUN_USER:-plex}"
  RUN_HOME="$(getent passwd "$RUN_USER" 2>/dev/null | cut -d: -f6 || true)"

  info "Stopping and removing ${SERVICE_NAME} service ..."
  systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
  rm -f "$UNIT_FILE"
  systemctl daemon-reload 2>/dev/null || true
  rm -rf "$INSTALL_DIR"
  ok "Removed ${SERVICE_NAME} service and ${INSTALL_DIR}"
  if [ -n "$RUN_HOME" ]; then
    info "Your data folder was kept at: ${RUN_HOME}/.cleanplex"
  fi
  exit 0
fi

RUN_USER="${RUN_USER:-plex}"
id "$RUN_USER" &>/dev/null || die "User '${RUN_USER}' does not exist."
[ -f "${SRC_DIR}/cleanplex/web/static/index.html" ] || die "Frontend not built (cleanplex/web/static missing). Run 'npm run build' in frontend/."
command -v curl >/dev/null 2>&1 || die "curl is required to install uv."

RUN_GROUP="$(id -gn "$RUN_USER")"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
[ -n "$RUN_HOME" ] || die "Could not determine home directory for '${RUN_USER}'."
LOG_FILE="${RUN_HOME}/.cleanplex/cleanplex.log"

# ── 1. Copy source ────────────────────────────────────────────────────────────
info "Syncing ${SRC_DIR} -> ${INSTALL_DIR} ..."
mkdir -p "$INSTALL_DIR"
# Excluded paths (.venv, .uv) are protected so the environment survives re-runs.
if command -v rsync &>/dev/null; then
  rsync -a --delete \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='.uv' \
    --exclude='build' \
    --exclude='frontend/node_modules' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='*.egg-info' \
    --exclude='.env' \
    "${SRC_DIR}/" "${INSTALL_DIR}/"
else
  find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 ! -name '.venv' ! -name '.uv' -exec rm -rf {} +
  (
    shopt -s dotglob nullglob
    for entry in "${SRC_DIR}"/*; do
      name="$(basename "$entry")"
      case "$name" in
        .git|.venv|.uv|build|__pycache__|*.egg-info|.env) continue ;;
      esac
      cp -a "$entry" "${INSTALL_DIR}/"
    done
  )
  rm -rf "${INSTALL_DIR}/frontend/node_modules"
fi
chown -R "${RUN_USER}:${RUN_GROUP}" "$INSTALL_DIR"
ok "Source synced"

# ── 2. Python env (as the service user) ───────────────────────────────────────
info "Setting up Python environment as '${RUN_USER}' (first run downloads Python + deps) ..."
sudo -u "$RUN_USER" -H bash -s -- "$INSTALL_DIR" <<'SETUP'
set -euo pipefail
cd "$1"
export UV_INSTALL_DIR="$1/.uv/bin"
export UV_PYTHON_INSTALL_DIR="$1/.uv/python"
export UV_CACHE_DIR="$1/.uv/cache"
export UV_NO_MODIFY_PATH=1
UV="${UV_INSTALL_DIR}/uv"
if [ ! -x "$UV" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
[ -x .venv/bin/python ] || "$UV" venv --python 3.12 .venv
"$UV" pip install --python .venv/bin/python --reinstall-package cleanplex .
SETUP
ok "Python environment ready ($("${INSTALL_DIR}/.venv/bin/python" --version))"

# ── 3. systemd unit ───────────────────────────────────────────────────────────
info "Writing ${UNIT_FILE} ..."
cat > "$UNIT_FILE" <<UNIT
[Unit]
Description=Cleanplex — Plex content filter service
Documentation=https://github.com/nazmolla/Cleanplex
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${INSTALL_DIR}/.venv/bin/cleanplex
WorkingDirectory=${INSTALL_DIR}
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}
User=${RUN_USER}
Group=${RUN_GROUP}
Environment="HOME=${RUN_HOME}"
Environment="CLEANPLEX_HOST=${HOST}"
Environment="CLEANPLEX_PORT=${PORT}"
Environment="CLEANPLEX_PROXY_HOST=127.0.0.1"
Environment="CLEANPLEX_LOG_FILE=${LOG_FILE}"

[Install]
WantedBy=multi-user.target
UNIT
chmod 644 "$UNIT_FILE"

# ── 4. Enable & start service ─────────────────────────────────────────────────
info "Starting ${SERVICE_NAME} service ..."
WAS_ACTIVE=0
if systemctl is-active --quiet "$SERVICE_NAME"; then
  WAS_ACTIVE=1
fi
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
if [ "$WAS_ACTIVE" -eq 1 ]; then
  systemctl restart "$SERVICE_NAME"
fi

for _ in $(seq 1 20); do
  curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/" 2>/dev/null && break
  sleep 1
done

if curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/" 2>/dev/null; then
  ok "Hayaa for Plex is running as '${RUN_USER}'"
else
  warn "Service not responding yet. Check: sudo journalctl -u ${SERVICE_NAME} -n 50"
fi

command -v ffmpeg &>/dev/null || \
  warn "ffmpeg not found — frame extraction / scanning will be unavailable."

LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
[ -n "$LAN_IP" ] || LAN_IP="localhost"

echo ""
echo "  Status:  sudo systemctl status ${SERVICE_NAME}"
echo "  Logs:    sudo journalctl -u ${SERVICE_NAME} -f"
echo "  Restart: sudo systemctl restart ${SERVICE_NAME}"
echo "  Stop:    sudo systemctl stop ${SERVICE_NAME}"
if [ "$HOST" = "127.0.0.1" ]; then
  echo "  Web UI:  http://localhost:${PORT}"
else
  echo "  Web UI:  http://${LAN_IP}:${PORT}"
fi
echo "  Data:    ${RUN_HOME}/.cleanplex"
