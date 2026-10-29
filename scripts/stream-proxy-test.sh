#!/bin/bash
# stream-proxy-test.sh — run the Plex stream proxy prototype (#132) for device testing.
#
# Starts the proxy as the Cleanplex service user against the LIVE database (read only, no
# migrations) and the local Plex, on port 7981, with --force-inject so clients that live
# skipping already reaches are edited too (otherwise the proxy would leave them alone).
#
#   sudo ./scripts/stream-proxy-test.sh start     # copy the source, run in the background
#   sudo ./scripts/stream-proxy-test.sh stop
#   sudo ./scripts/stream-proxy-test.sh status
#   sudo ./scripts/stream-proxy-test.sh log
#
# Then open  http://<this machine's LAN IP>:7981/web  in a browser and sign in to Plex.
# Nothing is installed: the source is copied to a temp directory and the service itself is
# not touched. Stop it when done.

set -euo pipefail

RUN_USER="${PROXY_USER:-server}"
PORT="${PROXY_PORT:-7981}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="/tmp/cleanplex-proxy-src"
PID_FILE="/tmp/cleanplex-stream-proxy.pid"
LOG_FILE="/tmp/cleanplex-stream-proxy.log"
VENV_PY="/opt/cleanplex/.venv/bin/python"
DB="$(dscl . -read "/Users/${RUN_USER}" NFSHomeDirectory | awk '{print $2}')/.cleanplex/cleanplex.db"

[ "$(id -u)" = "0" ] || { echo "Run with sudo." >&2; exit 1; }

running() { [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

case "${1:-}" in
  start)
    running && { echo "Already running (PID $(cat "$PID_FILE"))."; exit 0; }
    [ -x "$VENV_PY" ] || { echo "Missing $VENV_PY (is Cleanplex installed?)." >&2; exit 1; }
    rm -rf "$RUN_DIR"
    rsync -a --exclude .git --exclude .venv --exclude node_modules --exclude 'cleanplex/web/static' \
      --exclude __pycache__ "$SRC_DIR/" "$RUN_DIR/"
    chmod -R a+rX "$RUN_DIR"
    : > "$LOG_FILE"; chown "$RUN_USER" "$LOG_FILE"
    # Detached without nohup (which refuses to run without a terminal): ignore hangups, no stdin.
    ( trap '' HUP; exec sudo -H -u "$RUN_USER" env PYTHONPATH="$RUN_DIR" "$VENV_PY" -m cleanplex.stream_proxy \
        --db "$DB" --no-migrate --force-inject --port "$PORT" ) >> "$LOG_FILE" 2>&1 < /dev/null &
    echo $! > "$PID_FILE"
    for _ in $(seq 1 20); do
      curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/_proxy/report" 2>/dev/null && break
      sleep 1
    done
    if curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/_proxy/report" 2>/dev/null; then
      echo "Proxy running (PID $(cat "$PID_FILE")). Open one of:"
      grep "Plex Web through the proxy" "$LOG_FILE" || echo "  http://<this machine's LAN IP>:${PORT}/web"
    else
      echo "Did not start; see $LOG_FILE" >&2; tail -n 20 "$LOG_FILE" >&2; exit 1
    fi
    ;;
  stop)
    if running; then pkill -P "$(cat "$PID_FILE")" 2>/dev/null || true; kill "$(cat "$PID_FILE")" 2>/dev/null || true
      pkill -f "cleanplex.stream_proxy --db" 2>/dev/null || true; rm -f "$PID_FILE"; echo "Stopped."
    else pkill -f "cleanplex.stream_proxy --db" 2>/dev/null || true; rm -f "$PID_FILE"; echo "Not running."; fi
    ;;
  status) running && echo "Running (PID $(cat "$PID_FILE"))" || echo "Not running" ;;
  log)    tail -n 60 "$LOG_FILE" ;;
  *)      echo "Usage: sudo $0 {start|stop|status|log}" >&2; exit 2 ;;
esac
