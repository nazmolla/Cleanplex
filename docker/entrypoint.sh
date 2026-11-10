#!/bin/bash
# Cleanplex container entrypoint (#194): root-only setup, then drop to the Plex folder's owner.
set -euo pipefail

export CLEANPLEX_DATA="${CLEANPLEX_DATA:-/data}"
PLEX_PREFS="${CLEANPLEX_PLEX_DATA:-}/Library/Application Support/Plex Media Server/Preferences.xml"

# Run as whoever owns Plex's files, so Plex's private certificate is readable without root
# and media permissions match Plex's. PUID/PGID override; 1000 when there is nothing to copy.
if [ -z "${PUID:-}" ]; then
  if [ -n "${CLEANPLEX_PLEX_DATA:-}" ] && [ -f "$PLEX_PREFS" ]; then PUID="$(stat -c %u "$PLEX_PREFS")"; else PUID=1000; fi
fi
if [ -z "${PGID:-}" ]; then
  if [ -n "${CLEANPLEX_PLEX_DATA:-}" ] && [ -f "$PLEX_PREFS" ]; then PGID="$(stat -c %g "$PLEX_PREFS")"; else PGID=1000; fi
fi
echo "[entrypoint] running as uid=$PUID gid=$PGID"

mkdir -p "$CLEANPLEX_DATA"
chown -R "$PUID:$PGID" "$CLEANPLEX_DATA"

# Host-network mode only: send other machines' traffic for Plex's port to the front door,
# as scripts/frontdoor/install.sh does on Linux. Loopback is exempt so the proxy reaches Plex.
REDIRECT=0
if [ "${CLEANPLEX_REDIRECT:-}" = "nft" ]; then
  nft delete table inet cleanplex 2>/dev/null || true
  nft -f - <<NFT
table inet cleanplex {
  chain prerouting {
    type nat hook prerouting priority dstnat;
    tcp dport 32400 redirect to :32480
  }
  chain output {
    type nat hook output priority -100;
    ip daddr != 127.0.0.0/8 tcp dport 32400 redirect to :32480
    ip6 daddr != ::1 tcp dport 32400 redirect to :32480
  }
}
NFT
  REDIRECT=1
  echo "[entrypoint] nftables redirect 32400 -> 32480 active"
fi

cleanup() { [ "$REDIRECT" = "1" ] && nft delete table inet cleanplex 2>/dev/null || true; }
trap cleanup EXIT

export HOME=/tmp
setpriv --reuid="$PUID" --regid="$PGID" --clear-groups python -m cleanplex.container &
child=$!
trap 'kill -TERM "$child" 2>/dev/null || true' TERM INT
wait "$child" || rc=$?
wait "$child" 2>/dev/null || true
exit "${rc:-0}"
