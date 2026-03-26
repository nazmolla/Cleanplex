#!/usr/bin/env bash
# Smoke-test the dev instance before deploying to production.
# Exits non-zero if any check fails.
#
# Run this after frontend build and before restarting the prod server.

set -euo pipefail

BASE="http://localhost:7980"
PASS=0
FAIL=0

check() {
  local desc="$1"
  local url="$2"
  local expect="${3:-200}"
  local status
  # Use -s only (not -f) so curl doesn't exit non-zero on HTTP errors —
  # that would cause %{http_code} and the fallback echo to both append to status.
  status=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$url" 2>/dev/null)
  [[ -z "$status" ]] && status="000"
  if [[ "$status" == "$expect" ]]; then
    echo "  PASS  $desc ($status)"
    ((PASS++)) || true
  else
    echo "  FAIL  $desc — expected $expect, got $status"
    ((FAIL++)) || true
  fi
}

echo "=== Cleanplex dev smoke tests ==="

check "API /api/settings"              "$BASE/api/settings"
check "API /api/libraries"             "$BASE/api/libraries"
check "API /api/sessions"              "$BASE/api/sessions"
check "API /api/sessions/scanner-status" "$BASE/api/sessions/scanner-status"
check "Static index.html"             "$BASE/"

echo ""
echo "Results: $PASS passed, $FAIL failed"

if [[ $FAIL -gt 0 ]]; then
  echo "SMOKE TEST FAILED — do not deploy"
  exit 1
fi

echo "SMOKE TEST PASSED — safe to deploy"
