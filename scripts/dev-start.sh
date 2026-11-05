#!/usr/bin/env bash
# Start a dev instance of Cleanplex on port 7980.
#
# Uses ~/.cleanplex-dev/ as the data directory, seeded from the production
# DB so Plex credentials and settings are available without manual config.
# Works on macOS, Linux and Windows (Git Bash).
#
# Usage:
#   bash scripts/dev-start.sh           # start
#   bash scripts/dev-start.sh --fresh   # wipe dev DB and start clean
#
# CLEANPLEX_PROD_DB points at the production database when it is not in your own
# home folder (e.g. the macOS service runs as another account: sudo access needed).

set -euo pipefail

HOME_DIR="${USERPROFILE:-$HOME}"
PROD_DB="${CLEANPLEX_PROD_DB:-$HOME_DIR/.cleanplex/cleanplex.db}"
DEV_DIR="$HOME_DIR/.cleanplex-dev"
DEV_DB="$DEV_DIR/cleanplex.db"
DEV_LOG="cleanplex-dev.log"
PID_FILE=".dev-server.pid"

if [[ "${1:-}" == "--fresh" ]]; then
  echo "[dev] Wiping dev data dir $DEV_DIR"
  rm -rf "$DEV_DIR"
fi

mkdir -p "$DEV_DIR"

# Seed dev DB from production if it doesn't exist yet.
if [[ ! -f "$DEV_DB" ]]; then
  if [[ -r "$PROD_DB" ]]; then
    echo "[dev] Seeding dev DB from production ($PROD_DB)"
    cp "$PROD_DB" "$DEV_DB"
  elif [[ -e "$PROD_DB" ]] && command -v sudo >/dev/null; then
    echo "[dev] Seeding dev DB from production with sudo ($PROD_DB)"
    sudo cp "$PROD_DB" "$DEV_DB" && sudo chown "$(id -un)" "$DEV_DB"
  else
    echo "[dev] No production DB found — dev instance will start unconfigured"
  fi
else
  echo "[dev] Using existing dev DB at $DEV_DB"
fi

# Windows venvs keep executables in Scripts/, macOS and Linux in bin/.
if [[ -x .venv/Scripts/python.exe ]]; then VENV_PY=.venv/Scripts/python.exe; else VENV_PY=.venv/bin/python; fi

# A dev instance watches the same Plex as production. Its copy of the database must never
# run the enforcement backstop (it would stop real streams it did not proxy itself).
if [[ -f "$DEV_DB" ]]; then
  "$VENV_PY" -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\"INSERT INTO settings(key,value) VALUES('enforcement_enabled','0') ON CONFLICT(key) DO UPDATE SET value='0'\"); c.commit()" "$DEV_DB" \
    && echo "[dev] Enforcement backstop disabled in the dev database"
fi

# Kill any existing dev server.
if [[ -f "$PID_FILE" ]]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "[dev] Stopping existing dev server (PID $OLD_PID)"
    kill "$OLD_PID" 2>/dev/null || true
    sleep 2
  fi
  rm -f "$PID_FILE"
fi

if [[ -x .venv/Scripts/cleanplex.exe ]]; then CLEANPLEX_BIN=.venv/Scripts/cleanplex.exe; else CLEANPLEX_BIN=.venv/bin/cleanplex; fi

echo "[dev] Starting dev server on http://localhost:7980 (log: $DEV_LOG)"
# CLEANPLEX_PROXY_PORT=0: the dev instance runs no stream proxy, so it never clashes with production's.
CLEANPLEX_DATA="$DEV_DIR" CLEANPLEX_PORT=7980 CLEANPLEX_PROXY_PORT=0 \
  "$CLEANPLEX_BIN" > "$DEV_LOG" 2>&1 &
DEV_PID=$!
echo "$DEV_PID" > "$PID_FILE"
echo "[dev] PID $DEV_PID — waiting for startup..."

# Poll until the server responds (max 30s).
for i in $(seq 1 30); do
  sleep 1
  if curl -sf http://localhost:7980/api/settings > /dev/null 2>&1; then
    echo "[dev] Server is UP at http://localhost:7980"
    exit 0
  fi
done

echo "[dev] ERROR: server did not come up after 30s — check $DEV_LOG"
exit 1
