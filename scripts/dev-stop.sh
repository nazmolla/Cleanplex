#!/usr/bin/env bash
# Stop the dev Cleanplex instance started by dev-start.sh.

set -euo pipefail

PID_FILE=".dev-server.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "[dev] No PID file found — dev server may not be running"
  exit 0
fi

DEV_PID=$(cat "$PID_FILE")
if kill -0 "$DEV_PID" 2>/dev/null; then
  echo "[dev] Stopping dev server (PID $DEV_PID)"
  kill "$DEV_PID"
  rm -f "$PID_FILE"
  echo "[dev] Stopped"
else
  echo "[dev] PID $DEV_PID is not running — cleaning up PID file"
  rm -f "$PID_FILE"
fi
