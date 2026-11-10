#!/bin/bash
# Boot the image against a fake Plex and check the zero-config front door works (#194).
#   bash scripts/docker-smoke.sh            # uses image cleanplex:dev (docker build -t cleanplex:dev .)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
PY="${PYTHON:-.venv/bin/python}"; [ -x "$PY" ] || PY=python3
# Under the repo, not /tmp: Docker VMs (Colima, Docker Desktop) mount only the home folder.
export PLEX_CONFIG="$(mktemp -d "$PWD/.smoke-plex.XXXXXX")"
COMPOSE="docker compose -f docker/smoke/compose.yml"
fail() { echo "FAIL: $*"; $COMPOSE logs --tail 60 cleanplex || true; $COMPOSE down -v >/dev/null 2>&1; exit 1; }
trap '$COMPOSE down -v >/dev/null 2>&1; rm -rf "$PLEX_CONFIG"' EXIT

"$PY" docker/smoke/make_plex_config.py "$PLEX_CONFIG"
chmod -R a+rX "$PLEX_CONFIG"
$COMPOSE up -d
for _ in $(seq 1 60); do curl -fsS http://localhost:17979/api/settings >/dev/null 2>&1 && break; sleep 2; done
curl -fsS http://localhost:17979/api/settings >/dev/null || fail "web UI did not come up"

# Settings configured with no UI step.
curl -fsS http://localhost:17979/api/settings | grep -q '"plex_token_set": *true' || fail "Plex token not seeded"
curl -fsS http://localhost:17979/api/settings | grep -q 'http://127.0.0.1:32400' || fail "Plex URL not seeded"

# Front door answers on plain HTTP and TLS (Plex's certificate) on the one port.
for _ in $(seq 1 40); do curl -fsS http://localhost:33400/identity 2>/dev/null | grep -q smoke-mid && break; sleep 2; done
curl -fsS http://localhost:33400/identity | grep -q smoke-mid || fail "plain HTTP through the front door failed"
curl -fsSk https://localhost:33400/identity | grep -q smoke-mid || fail "TLS through the front door failed"
curl -sk -o /dev/null -w '%{ssl_verify_result}' https://localhost:33400/identity >/dev/null

# First-run configuration: enforcement on, Relay and DLNA off.
for _ in $(seq 1 30); do curl -fsS http://localhost:17979/api/enforcement | grep -q '"enabled": *true' && break; sleep 2; done
curl -fsS http://localhost:17979/api/enforcement | grep -q '"enabled": *true' || fail "enforcement not enabled"
LOG="$(curl -fsS http://localhost:33400/_stub/prefs-log)"
echo "$LOG" | grep -q '"RelayEnabled": "0"' || fail "Relay not switched off: $LOG"
echo "$LOG" | grep -q '"DlnaEnabled": "0"' || fail "DLNA not switched off: $LOG"

# A restart keeps what the user may have changed: the first-run step is not repeated.
curl -fsS -X POST http://localhost:17979/api/enforcement -H 'Content-Type: application/json' -d '{"enabled": false}' >/dev/null
$COMPOSE restart cleanplex
for _ in $(seq 1 60); do curl -fsS http://localhost:17979/api/settings >/dev/null 2>&1 && break; sleep 2; done
sleep 20
curl -fsS http://localhost:17979/api/enforcement | grep -q '"enabled": *false' || fail "restart re-enabled enforcement"
curl -fsS http://localhost:33400/identity | grep -q smoke-mid || fail "front door down after restart"
echo "PASS: Docker smoke test"
