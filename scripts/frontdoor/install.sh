#!/bin/bash
# frontdoor/install.sh — put Cleanplex's stream proxy in front of Plex (#184), Mode A.
#
# Every client reaching Plex's port 32400 (LAN, remote through your router, this machine)
# is redirected to an nginx front on port 32480. nginx terminates TLS with Plex's own
# certificate, streams video from Plex, and asks Cleanplex only about playback, playlists
# and subtitles. Plex itself is untouched and stays on 127.0.0.1:32400 for the proxy.
#
#   sudo bash scripts/frontdoor/install.sh [--user server]       # install / update
#   sudo bash scripts/frontdoor/install.sh --uninstall             # remove everything
#
# Supports macOS (pf + LaunchDaemon) and Linux (nftables + systemd). Docker and Windows
# setups are described in README.md (Docker: map the host's 32400 to the front port;
# Windows: Mode B with scripts/frontdoor/install-windows.ps1).

set -euo pipefail

FRONT_PORT=32480
PLEX_PORT=32400
RUN_USER=""
UNINSTALL=0
CLEANPLEX_URL="${CLEANPLEX_URL:-http://127.0.0.1:7979}"

while [ $# -gt 0 ]; do
  case "$1" in
    --user) RUN_USER="$2"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

info() { printf '\033[1;34m[INFO]\033[0m  %s\n' "$*"; }
ok()   { printf '\033[1;32m[OK]\033[0m    %s\n' "$*"; }
die()  { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "Run with sudo."
OS="$(uname -s)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# The account that runs Plex and Cleanplex: it can read Plex's certificate and preferences.
if [ -z "$RUN_USER" ]; then
  if [ "$OS" = "Darwin" ]; then RUN_USER="server"; else RUN_USER="plex"; fi
fi
id "$RUN_USER" &>/dev/null || die "User '$RUN_USER' does not exist (use --user)."
if [ "$OS" = "Darwin" ]; then
  RUN_HOME="$(dscl . -read "/Users/${RUN_USER}" NFSHomeDirectory | awk '{print $2}')"
else
  RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
fi
DATA_DIR="${CLEANPLEX_DATA:-$RUN_HOME/.cleanplex}"
CONF="$DATA_DIR/frontdoor/nginx.conf"
# -e: nginx opens its built-in error log before reading the config; point it at ours instead.
ELOG="$DATA_DIR/frontdoor/cleanplex-nginx-error.log"
PYTHON="${CLEANPLEX_PYTHON:-/opt/cleanplex/.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON="$SRC_DIR/.venv/bin/python"

set_enforcement() {  # tell Cleanplex whether every client now passes the proxy (enables the backstop)
  curl -fsS -m 10 -X POST "$CLEANPLEX_URL/api/enforcement" -H 'Content-Type: application/json' \
       -d "{\"enabled\": $1}" >/dev/null && ok "Cleanplex enforcement set to $1" \
       || printf '\033[1;33m[WARN]\033[0m  Could not reach Cleanplex at %s; set enforcement in Settings.\n' "$CLEANPLEX_URL"
}

# ── macOS ──────────────────────────────────────────────────────────────────────
mac_uninstall() {
  launchctl bootout system/com.homeserv.cleanplex-frontdoor 2>/dev/null || true
  launchctl bootout system/com.homeserv.cleanplex-pf 2>/dev/null || true
  rm -f /Library/LaunchDaemons/com.homeserv.cleanplex-frontdoor.plist /Library/LaunchDaemons/com.homeserv.cleanplex-pf.plist
  pfctl -a com.apple/cleanplex -F all 2>/dev/null || true
  if [ -f /etc/pf.anchors/cleanplex.token ]; then pfctl -X "$(cat /etc/pf.anchors/cleanplex.token)" 2>/dev/null || true; fi
  rm -f /etc/pf.anchors/cleanplex /etc/pf.anchors/cleanplex.token /usr/local/sbin/cleanplex-pf.sh
  ok "Redirect and front door removed; clients reach Plex directly again"
}

mac_install() {
  NGINX="$(command -v nginx || echo /opt/homebrew/bin/nginx)"
  [ -x "$NGINX" ] || die "nginx not found. Install it: brew install nginx"

  info "Extracting Plex's certificate and writing the nginx config ..."
  sudo -u "$RUN_USER" -H env PYTHONPATH="$SRC_DIR" "$PYTHON" -m cleanplex.frontdoor "$DATA_DIR" >/dev/null
  sudo -u "$RUN_USER" "$NGINX" -e "$ELOG" -t -c "$CONF" -q || die "nginx rejected $CONF"

  info "Installing the nginx front (LaunchDaemon) ..."
  cat > /Library/LaunchDaemons/com.homeserv.cleanplex-frontdoor.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.homeserv.cleanplex-frontdoor</string>
  <key>ProgramArguments</key><array>
    <string>${NGINX}</string><string>-e</string><string>${ELOG}</string><string>-c</string><string>${CONF}</string><string>-g</string><string>daemon off;</string>
  </array>
  <key>UserName</key><string>${RUN_USER}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>${DATA_DIR}/frontdoor/nginx-stderr.log</string>
</dict></plist>
PLIST
  launchctl bootout system/com.homeserv.cleanplex-frontdoor 2>/dev/null || true
  for _ in $(seq 1 20); do launchctl print system/com.homeserv.cleanplex-frontdoor >/dev/null 2>&1 || break; sleep 1; done
  launchctl bootstrap system /Library/LaunchDaemons/com.homeserv.cleanplex-frontdoor.plist
  for _ in $(seq 1 15); do curl -fsS -o /dev/null "http://127.0.0.1:${FRONT_PORT}/identity" && break; sleep 1; done
  curl -fsS -o /dev/null "http://127.0.0.1:${FRONT_PORT}/identity" || die "The front door does not answer on ${FRONT_PORT}"
  ok "Front door answering on ${FRONT_PORT}"

  info "Installing the redirect (pf anchor com.apple/cleanplex) ..."
  # Uses macOS's existing rdr-anchor "com.apple/*", so /etc/pf.conf is not edited.
  # Rules are written at boot for every interface with an address; (if) follows DHCP changes.
  cat > /usr/local/sbin/cleanplex-pf.sh <<SH
#!/bin/bash
# Generated by Cleanplex frontdoor/install.sh: redirect Plex's port to the Cleanplex front door.
RULES=/etc/pf.anchors/cleanplex
: > "\$RULES"
for IF in \$(ifconfig -l); do
  case "\$IF" in lo*|gif*|stf*|utun*|awdl*|llw*|anpi*|ap*|nan*|bridge*) continue ;; esac
  # Redirect to the interface's own address, not loopback: macOS drops traffic from other
  # machines redirected to 127.0.0.1 (verified from a phone, 2026-11-05).
  ifconfig "\$IF" 2>/dev/null | grep -q "inet " && echo "rdr pass on \$IF inet proto tcp from any to (\$IF) port ${PLEX_PORT} -> (\$IF) port ${FRONT_PORT}" >> "\$RULES"
  ifconfig "\$IF" 2>/dev/null | grep -q "inet6 " && echo "rdr pass on \$IF inet6 proto tcp from any to (\$IF) port ${PLEX_PORT} -> (\$IF) port ${FRONT_PORT}" >> "\$RULES"
  # This machine's own requests to its LAN address travel over lo0.
  for ADDR in \$(ifconfig "\$IF" 2>/dev/null | awk '/inet /{print \$2}'); do
    echo "rdr pass on lo0 inet proto tcp from any to \$ADDR port ${PLEX_PORT} -> 127.0.0.1 port ${FRONT_PORT}" >> "\$RULES"
  done
done
# Enable pf once (it runs again on every network change); keep the token so uninstall releases it.
pfctl -s info 2>/dev/null | grep -q "Status: Enabled" || pfctl -E 2>&1 | awk '/Token/{print \$3}' > /etc/pf.anchors/cleanplex.token
pfctl -a com.apple/cleanplex -f "\$RULES" 2>/dev/null
SH
  chmod 755 /usr/local/sbin/cleanplex-pf.sh
  cat > /Library/LaunchDaemons/com.homeserv.cleanplex-pf.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.homeserv.cleanplex-pf</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>/usr/local/sbin/cleanplex-pf.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>WatchPaths</key><array><string>/Library/Preferences/SystemConfiguration</string></array>
</dict></plist>
PLIST
  launchctl bootout system/com.homeserv.cleanplex-pf 2>/dev/null || true
  launchctl bootstrap system /Library/LaunchDaemons/com.homeserv.cleanplex-pf.plist
  sleep 2
  pfctl -a com.apple/cleanplex -s nat 2>/dev/null | grep -q "port = ${PLEX_PORT}" || die "pf redirect did not load"
  ok "Redirect active: $(pfctl -a com.apple/cleanplex -s nat 2>/dev/null | wc -l | tr -d ' ') rule(s)"
}

# ── Linux ──────────────────────────────────────────────────────────────────────
linux_uninstall() {
  systemctl disable --now cleanplex-frontdoor.service cleanplex-redirect.service 2>/dev/null || true
  rm -f /etc/systemd/system/cleanplex-frontdoor.service /etc/systemd/system/cleanplex-redirect.service
  systemctl daemon-reload
  nft delete table inet cleanplex 2>/dev/null || true
  ok "Redirect and front door removed; clients reach Plex directly again"
}

linux_install() {
  NGINX="$(command -v nginx)" || die "nginx not found. Install it (apt install nginx-full / dnf install nginx nginx-mod-stream)."
  command -v nft >/dev/null || die "nftables (nft) not found."
  info "Extracting Plex's certificate and writing the nginx config ..."
  sudo -u "$RUN_USER" -H env PYTHONPATH="$SRC_DIR" "$PYTHON" -m cleanplex.frontdoor "$DATA_DIR" >/dev/null
  sudo -u "$RUN_USER" "$NGINX" -e "$ELOG" -t -c "$CONF" -q || die "nginx rejected $CONF"
  cat > /etc/systemd/system/cleanplex-frontdoor.service <<UNIT
[Unit]
Description=Cleanplex front door (nginx) in front of Plex
After=network-online.target
[Service]
User=${RUN_USER}
ExecStart=${NGINX} -e ${ELOG} -c ${CONF} -g 'daemon off;'
ExecReload=${NGINX} -e ${ELOG} -c ${CONF} -s reload
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
  # prerouting: traffic from other machines; output: this machine's own requests to a
  # non-loopback address. Loopback is exempt so the proxy still reaches Plex directly.
  cat > /etc/cleanplex-redirect.nft <<NFT
table inet cleanplex {
  chain prerouting {
    type nat hook prerouting priority dstnat;
    tcp dport ${PLEX_PORT} redirect to :${FRONT_PORT}
  }
  chain output {
    type nat hook output priority -100;
    ip daddr != 127.0.0.0/8 tcp dport ${PLEX_PORT} redirect to :${FRONT_PORT}
    ip6 daddr != ::1 tcp dport ${PLEX_PORT} redirect to :${FRONT_PORT}
  }
}
NFT
  cat > /etc/systemd/system/cleanplex-redirect.service <<UNIT
[Unit]
Description=Cleanplex: redirect Plex's port to the front door
After=network-online.target cleanplex-frontdoor.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=-/usr/sbin/nft delete table inet cleanplex
ExecStart=/usr/sbin/nft -f /etc/cleanplex-redirect.nft
ExecStop=/usr/sbin/nft delete table inet cleanplex
[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now cleanplex-frontdoor.service
  for _ in $(seq 1 15); do curl -fsS -o /dev/null "http://127.0.0.1:${FRONT_PORT}/identity" && break; sleep 1; done
  curl -fsS -o /dev/null "http://127.0.0.1:${FRONT_PORT}/identity" || die "The front door does not answer on ${FRONT_PORT}"
  systemctl enable --now cleanplex-redirect.service
  ok "Front door and redirect active"
}

if [ "$UNINSTALL" = "1" ]; then
  set_enforcement false
  if [ "$OS" = "Darwin" ]; then mac_uninstall; else linux_uninstall; fi
  exit 0
fi

if [ "$OS" = "Darwin" ]; then mac_install; elif [ "$OS" = "Linux" ]; then linux_install; else die "Unsupported OS: $OS"; fi
set_enforcement true
echo ""
echo "  Undo:   sudo bash scripts/frontdoor/install.sh --uninstall"
echo "  Plex:   turn Relay off (Settings → Network) — relayed streams bypass the front door"
