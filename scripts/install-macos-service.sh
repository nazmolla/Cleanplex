#!/bin/bash
# install-macos-service.sh — Install Cleanplex as a macOS LaunchDaemon that runs
# as a regular user (mirrors /Library/LaunchDaemons/com.homeserv.plex.plist).
#
# Usage:
#   sudo ./scripts/install-macos-service.sh [run_user]
#
#   run_user  (optional) account to run the service as, defaults to "server"
#
# Re-running the script updates the installed code and restarts the service.
# Python 3.12 is provisioned via uv inside the install dir, so no system Python
# or Homebrew is required. The prebuilt frontend in cleanplex/web/static is used.

set -euo pipefail

RUN_USER="${1:-server}"
LABEL="com.homeserv.cleanplex"
INSTALL_DIR="/opt/cleanplex"
PLIST="/Library/LaunchDaemons/${LABEL}.plist"
PORT="7979"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

info() { printf '\033[1;34m[INFO]\033[0m  %s\n' "$*"; }
ok()   { printf '\033[1;32m[OK]\033[0m    %s\n' "$*"; }
die()  { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "Run with sudo: sudo $0 ${RUN_USER}"
id "$RUN_USER" &>/dev/null || die "User '${RUN_USER}' does not exist."
[ -f "${SRC_DIR}/cleanplex/web/static/index.html" ] || die "Frontend not built (cleanplex/web/static missing). Run 'npm run build' in frontend/."

RUN_GROUP="$(id -gn "$RUN_USER")"
RUN_HOME="$(dscl . -read "/Users/${RUN_USER}" NFSHomeDirectory | awk '{print $2}')"
# The app writes a size-rotated log here; launchd's own stdout/stderr files only catch crashes (#158).
LOG_FILE="${RUN_HOME}/.cleanplex/cleanplex.log"
CRASH_LOG="/tmp/homeserv-cleanplex.err"

# ── 1. Copy source ────────────────────────────────────────────────────────────
info "Syncing ${SRC_DIR} → ${INSTALL_DIR} ..."
mkdir -p "$INSTALL_DIR"
# Excluded paths (.venv, .uv) are also protected from --delete, so the
# environment survives re-runs.
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

# ── 3. LaunchDaemon ───────────────────────────────────────────────────────────
info "Writing ${PLIST} ..."
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${INSTALL_DIR}/.venv/bin/cleanplex</string>
  </array>
  <key>WorkingDirectory</key><string>${INSTALL_DIR}</string>
  <key>UserName</key><string>${RUN_USER}</string>
  <key>GroupName</key><string>${RUN_GROUP}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>${RUN_HOME}</string>
    <key>USER</key><string>${RUN_USER}</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>CLEANPLEX_PORT</key><string>${PORT}</string>
    <key>CLEANPLEX_LOG_FILE</key><string>${LOG_FILE}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>${CRASH_LOG}</string>
</dict>
</plist>
PLIST
chown root:wheel "$PLIST"
chmod 644 "$PLIST"
plutil -lint "$PLIST" >/dev/null || die "Generated plist is invalid."

# ── 4. (Re)load service ───────────────────────────────────────────────────────
info "Loading service ..."
launchctl bootout "system/${LABEL}" 2>/dev/null || true
launchctl enable "system/${LABEL}"
launchctl bootstrap system "$PLIST"

for _ in $(seq 1 20); do
  curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/" 2>/dev/null && break
  sleep 1
done

if curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/" 2>/dev/null; then
  ok "Cleanplex is running as '${RUN_USER}'"
else
  printf '\033[1;33m[WARN]\033[0m  Service not responding yet. Check: tail -n 50 %s\n' "$LOG_FILE"
fi

command -v ffmpeg &>/dev/null || [ -x /opt/homebrew/bin/ffmpeg ] || \
  printf '\033[1;33m[WARN]\033[0m  ffmpeg not found — frame extraction / scanning will be unavailable.\n'

echo ""
echo "  Status:  sudo launchctl print system/${LABEL} | head -20"
echo "  Logs:    tail -f ${LOG_FILE}"
echo "  Restart: sudo launchctl kickstart -k system/${LABEL}"
echo "  Stop:    sudo launchctl bootout system/${LABEL}"
echo "  Web UI:  http://$(ipconfig getifaddr en0 2>/dev/null || echo localhost):${PORT}"
echo "  Data:    ${RUN_HOME}/.cleanplex"
