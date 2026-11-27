#!/bin/sh
# One-line installer / updater / uninstaller for Hayaa for Plex (formerly Cleanplex).
# Usage:
#   curl -fsSL https://nazmolla.github.io/Cleanplex/install.sh | sh
#   curl -fsSL https://nazmolla.github.io/Cleanplex/install.sh | sh -s -- --allow-network
#   curl -fsSL https://nazmolla.github.io/Cleanplex/install.sh | sh -s -- --uninstall

set -eu

DEFAULT_BUNDLE_URL="https://github.com/nazmolla/Cleanplex/releases/latest/download/hayaa-for-plex.tar.gz"
INSTALL_DOCS_URL="https://nazmolla.github.io/Cleanplex/getting-started/installation/"
DOCKER_DOCS_URL="https://nazmolla.github.io/Cleanplex/getting-started/installation/#install-with-docker-recommended"
INSTALL_DIR="/opt/cleanplex"
MAC_LABEL="com.homeserv.cleanplex"
MAC_PLIST="/Library/LaunchDaemons/${MAC_LABEL}.plist"
LINUX_UNIT="/etc/systemd/system/cleanplex.service"
PORT="7979"

if [ -t 1 ]; then
  C_BLUE="$(printf '\033[1;34m')"
  C_GREEN="$(printf '\033[1;32m')"
  C_YELLOW="$(printf '\033[1;33m')"
  C_RED="$(printf '\033[1;31m')"
  C_BOLD="$(printf '\033[1m')"
  C_RESET="$(printf '\033[0m')"
else
  C_BLUE=''
  C_GREEN=''
  C_YELLOW=''
  C_RED=''
  C_BOLD=''
  C_RESET=''
fi

info() { printf '%s==>%s %s\n' "$C_BLUE" "$C_RESET" "$*"; }
ok()   { printf '%s✓%s   %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '%s!%s   %s\n' "$C_YELLOW" "$C_RESET" "$*"; }
die()  { printf '%s✗%s   %s\n' "$C_RED" "$C_RESET" "$*" >&2; exit 1; }

UNINSTALL=0
ALLOW_NETWORK=0
FRONTDOOR_CHOICE=""
case "${HAYAA_FRONTDOOR:-}" in
  1|true|yes) FRONTDOOR_CHOICE="yes" ;;
  0|false|no) FRONTDOOR_CHOICE="no" ;;
esac

while [ $# -gt 0 ]; do
  case "$1" in
    --uninstall)
      UNINSTALL=1
      shift
      ;;
    --allow-network)
      ALLOW_NETWORK=1
      shift
      ;;
    --frontdoor)
      FRONTDOOR_CHOICE="yes"
      shift
      ;;
    --no-frontdoor)
      FRONTDOOR_CHOICE="no"
      shift
      ;;
    -h|--help)
      echo "Usage: install.sh [--allow-network] [--frontdoor | --no-frontdoor] [--uninstall]"
      exit 0
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
done

run_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    command -v sudo >/dev/null 2>&1 || die "Administrator access (sudo) is needed, but 'sudo' was not found."
    sudo "$@"
  fi
}

OS="$(uname -s)"
case "$OS" in
  Darwin|Linux) ;;
  *)
    printf '%s✗%s   Sorry, this installer only supports macOS and Linux (detected: %s).\n' "$C_RED" "$C_RESET" "$OS" >&2
    printf "    See all install options at: %s\n" "$INSTALL_DOCS_URL" >&2
    exit 1
    ;;
esac

# ── Helper: find invoking user & home ─────────────────────────────────────────
invoking_user() {
  if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    printf '%s\n' "$SUDO_USER"
  elif [ "$(id -u)" -ne 0 ]; then
    id -un
  elif [ "$OS" = "Darwin" ]; then
    cu="$(stat -f %Su /dev/console 2>/dev/null || true)"
    if [ -n "$cu" ] && [ "$cu" != "root" ] && [ "$cu" != "loginwindow" ]; then
      printf '%s\n' "$cu"
    else
      id -un
    fi
  else
    id -un
  fi
}

user_home() {
  if [ "$OS" = "Darwin" ]; then
    dscl . -read "/Users/${1}" NFSHomeDirectory 2>/dev/null | awk '{print $2}' || true
  else
    getent passwd "$1" 2>/dev/null | cut -d: -f6 || true
  fi
}

# ── Uninstall ─────────────────────────────────────────────────────────────────
if [ "$UNINSTALL" -eq 1 ]; then
  info "Uninstalling Hayaa for Plex..."
  DATA_HOME=""
  if [ "$OS" = "Darwin" ]; then
    if [ -f "$MAC_PLIST" ]; then
      DATA_HOME="$(plutil -extract EnvironmentVariables.HOME raw "$MAC_PLIST" 2>/dev/null || true)"
    fi
    run_root launchctl bootout "system/${MAC_LABEL}" 2>/dev/null || true
    run_root rm -f "$MAC_PLIST"
  else
    if [ -f "$LINUX_UNIT" ]; then
      UNIT_USER="$(sed -n 's/^User=//p' "$LINUX_UNIT" | head -n 1 || true)"
      if [ -n "$UNIT_USER" ]; then
        DATA_HOME="$(user_home "$UNIT_USER")"
      fi
    fi
    run_root systemctl disable --now cleanplex 2>/dev/null || true
    run_root rm -f "$LINUX_UNIT"
    run_root systemctl daemon-reload 2>/dev/null || true
  fi
  run_root rm -rf "$INSTALL_DIR"

  if [ -z "$DATA_HOME" ]; then
    DATA_HOME="$(user_home "$(invoking_user)")"
  fi
  ok "Hayaa for Plex has been removed."
  if [ -n "$DATA_HOME" ]; then
    info "Your settings and scanned library were kept in: ${DATA_HOME}/.cleanplex"
  else
    info "Your ~/.cleanplex data folder was kept untouched."
  fi
  exit 0
fi

# ── Platform checks (Unraid / Docker Plex) ────────────────────────────────────
if [ "$OS" = "Linux" ] && [ -e /etc/unraid-version ]; then
  info "Unraid detected!"
  echo "  On Unraid, please install \"Cleanplex\" directly from Community Apps instead."
  echo "  More info: ${DOCKER_DOCS_URL}"
  exit 0
fi

# ── Find Plex's account (same computer is required) ───────────────────────────
# The running server process is the most reliable answer: Plex often runs as a different
# account than the person logged in, and old Plex settings files can linger in other homes.
PLEX_USER="$(ps -eo user=,args= 2>/dev/null | awk '/\/Plex Media Server( |$)/ {print $1; exit}' || true)"
if [ -n "$PLEX_USER" ] && ! id "$PLEX_USER" >/dev/null 2>&1; then
  PLEX_USER=""
fi
if [ -n "$PLEX_USER" ]; then
  :
elif [ "$OS" = "Darwin" ]; then
  CONSOLE_USER="$(stat -f %Su /dev/console 2>/dev/null || true)"
  if [ -n "$CONSOLE_USER" ] && [ "$CONSOLE_USER" != "root" ] && [ "$CONSOLE_USER" != "loginwindow" ]; then
    CONSOLE_HOME="$(user_home "$CONSOLE_USER")"
    PLIST_PATH="${CONSOLE_HOME}/Library/Preferences/com.plexapp.plexmediaserver.plist"
    if [ -n "$CONSOLE_HOME" ] && [ -f "$PLIST_PATH" ]; then
      CANDIDATE="$(stat -f %Su "$PLIST_PATH" 2>/dev/null || true)"
      if [ -n "$CANDIDATE" ] && id "$CANDIDATE" >/dev/null 2>&1; then
        PLEX_USER="$CANDIDATE"
      fi
    fi
  fi
else
  if [ -d /var/lib/plexmediaserver ]; then
    CANDIDATE="$(stat -c %U /var/lib/plexmediaserver 2>/dev/null || true)"
    if [ -n "$CANDIDATE" ] && id "$CANDIDATE" >/dev/null 2>&1; then
      PLEX_USER="$CANDIDATE"
    fi
  fi
fi

if [ -n "$PLEX_USER" ]; then
  ok "Found Plex Media Server (running as '${PLEX_USER}')."
else
  # Not found natively — check if Plex is running in Docker
  DOCKER_PLEX=""
  if command -v docker >/dev/null 2>&1; then
    DOCKER_IMAGES="$(docker ps --format '{{.Image}}' 2>/dev/null || sudo -n docker ps --format '{{.Image}}' 2>/dev/null || true)"
    if printf '%s\n' "$DOCKER_IMAGES" | grep -qiE 'plexinc/pms-docker|linuxserver/plex'; then
      DOCKER_PLEX="1"
    fi
  fi

  if [ -n "$DOCKER_PLEX" ]; then
    info "Plex appears to be running in Docker on this machine."
    echo "  The Docker setup of Hayaa for Plex is the right choice here:"
    echo "  ${DOCKER_DOCS_URL}"
    exit 0
  fi

  printf '%s✗%s   Plex Media Server was not found on this computer. Install Hayaa for Plex on the computer that runs Plex.\n' "$C_RED" "$C_RESET" >&2
  printf "    See all install options: %s\n" "$INSTALL_DOCS_URL" >&2
  exit 1
fi

# ── Network binding choice ────────────────────────────────────────────────────
if [ "$ALLOW_NETWORK" -eq 1 ]; then
  HAYAA_HOST_VAL="0.0.0.0"
  warn "Network access enabled: anyone on your network can open and change Hayaa for Plex because it has no login."
else
  HAYAA_HOST_VAL="127.0.0.1"
fi

# ── Helper: locate / install Homebrew formula on macOS ────────────────────────
find_brew() {
  brew_cmd="$(command -v brew 2>/dev/null || true)"
  if [ -z "$brew_cmd" ] && [ -x /opt/homebrew/bin/brew ]; then
    brew_cmd="/opt/homebrew/bin/brew"
  elif [ -z "$brew_cmd" ] && [ -x /usr/local/bin/brew ]; then
    brew_cmd="/usr/local/bin/brew"
  fi
  printf '%s\n' "$brew_cmd"
}

brew_install_pkg() {
  pkg="$1"
  brew_cmd="$(find_brew)"
  [ -n "$brew_cmd" ] || return 1
  if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    sudo -u "$SUDO_USER" "$brew_cmd" install "$pkg"
  elif [ "$(id -u)" -ne 0 ]; then
    "$brew_cmd" install "$pkg"
  else
    return 1
  fi
}

# ── Ensure ffmpeg / ffprobe ───────────────────────────────────────────────────
has_ffmpeg() {
  if [ "$OS" = "Darwin" ]; then
    (command -v ffmpeg >/dev/null 2>&1 || [ -x /opt/homebrew/bin/ffmpeg ] || [ -x /usr/local/bin/ffmpeg ]) && \
    (command -v ffprobe >/dev/null 2>&1 || [ -x /opt/homebrew/bin/ffprobe ] || [ -x /usr/local/bin/ffprobe ])
  else
    command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1
  fi
}

if ! has_ffmpeg; then
  info "Installing ffmpeg (needed to scan videos)..."
  if [ "$OS" = "Darwin" ]; then
    if [ -n "$(find_brew)" ]; then
      brew_install_pkg ffmpeg || warn "Could not install ffmpeg with Homebrew. Install it later with: brew install ffmpeg"
    else
      warn "ffmpeg is not installed and Homebrew was not found."
      echo "  Install Homebrew from https://brew.sh and run: brew install ffmpeg"
    fi
  else
    if command -v apt-get >/dev/null 2>&1; then
      if ! (run_root apt-get update -qq && run_root apt-get install -y ffmpeg); then
        warn "Could not install ffmpeg automatically. Please install ffmpeg."
      fi
    elif command -v dnf >/dev/null 2>&1; then
      run_root dnf install -y ffmpeg || warn "Could not install ffmpeg automatically. Please install ffmpeg."
    elif command -v pacman >/dev/null 2>&1; then
      run_root pacman -Sy --noconfirm ffmpeg || warn "Could not install ffmpeg automatically. Please install ffmpeg."
    elif command -v zypper >/dev/null 2>&1; then
      run_root zypper --non-interactive install ffmpeg || warn "Could not install ffmpeg automatically. Please install ffmpeg."
    else
      warn "ffmpeg is not installed. Please install ffmpeg using your system package manager."
    fi
  fi
fi

# ── Download & extract bundle ─────────────────────────────────────────────────
BUNDLE_URL="${HAYAA_BUNDLE_URL:-$DEFAULT_BUNDLE_URL}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT INT TERM
TAR_FILE="${TMP_DIR}/hayaa-for-plex.tar.gz"
EXTRACT_DIR="${TMP_DIR}/bundle"
mkdir -p "$EXTRACT_DIR"

info "Downloading Hayaa for Plex..."
if [ -f "$BUNDLE_URL" ]; then
  cp "$BUNDLE_URL" "$TAR_FILE"
elif command -v curl >/dev/null 2>&1; then
  curl -fsSL "$BUNDLE_URL" -o "$TAR_FILE"
elif command -v wget >/dev/null 2>&1; then
  wget -qO "$TAR_FILE" "$BUNDLE_URL"
else
  die "Neither curl nor wget is installed. Please install curl and try again."
fi

tar -xzf "$TAR_FILE" -C "$EXTRACT_DIR"
BUNDLE_DIR="$EXTRACT_DIR"
if [ ! -f "${BUNDLE_DIR}/pyproject.toml" ]; then
  SUB_DIR="$(find "$EXTRACT_DIR" -mindepth 1 -maxdepth 1 -type d | head -n 1 || true)"
  if [ -n "$SUB_DIR" ] && [ -f "${SUB_DIR}/pyproject.toml" ]; then
    BUNDLE_DIR="$SUB_DIR"
  fi
fi

# ── Install & start service ───────────────────────────────────────────────────
info "Installing Hayaa for Plex service (this takes a minute on first install)..."
if [ "$OS" = "Darwin" ]; then
  run_root env HAYAA_HOST="$HAYAA_HOST_VAL" bash "${BUNDLE_DIR}/scripts/install-macos-service.sh" "$PLEX_USER"
else
  run_root env HAYAA_HOST="$HAYAA_HOST_VAL" bash "${BUNDLE_DIR}/scripts/install-linux-service.sh" "$PLEX_USER"
fi

# ── Wait for Web UI ───────────────────────────────────────────────────────────
info "Waiting for Hayaa for Plex to start..."
READY=0
i=0
while [ "$i" -lt 60 ]; do
  if command -v curl >/dev/null 2>&1; then
    if curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/" 2>/dev/null; then
      READY=1
      break
    fi
  elif command -v wget >/dev/null 2>&1; then
    if wget -q --spider "http://127.0.0.1:${PORT}/" 2>/dev/null; then
      READY=1
      break
    fi
  fi
  sleep 1
  i=$((i + 1))
done

# ── Optional front door (filter every Plex client) ────────────────────────────
echo ""
echo "  The front door opens nothing new to the internet: clients keep using Plex's usual port."
if [ -z "$FRONTDOOR_CHOICE" ]; then
  ANS=""
  if [ -r /dev/tty ]; then
    printf "  Also filter Plex on every device (TVs, phones, web)? This routes Plex's port 32400 through Hayaa's front door. [Y/n] "
    IFS= read -r ANS </dev/tty || ANS=""
  fi
  # Yes unless the user says no: without the front door only some Plex apps can be filtered.
  case "$ANS" in
    [nN]|[nN][oO]) FRONTDOOR_CHOICE="no" ;;
    *) FRONTDOOR_CHOICE="yes" ;;
  esac
fi

if [ "$FRONTDOOR_CHOICE" = "yes" ]; then
  info "Setting up Hayaa's front door..."
  NGINX_OK=0
  if [ "$OS" = "Darwin" ]; then
    if command -v nginx >/dev/null 2>&1 || [ -x /opt/homebrew/bin/nginx ] || [ -x /usr/local/bin/nginx ]; then
      NGINX_OK=1
    elif [ -n "$(find_brew)" ]; then
      if brew_install_pkg nginx; then
        NGINX_OK=1
      fi
    fi
    if [ "$NGINX_OK" -eq 0 ]; then
      warn "Could not install nginx automatically, so the front door was skipped. Install nginx with Homebrew (brew install nginx) and run: sudo bash /opt/cleanplex/scripts/frontdoor/install.sh --user ${PLEX_USER}"
    fi
  else
    if command -v nginx >/dev/null 2>&1; then
      NGINX_OK=1
    elif command -v apt-get >/dev/null 2>&1; then
      if run_root apt-get update -qq && run_root apt-get install -y nginx-full; then
        NGINX_OK=1
      fi
    elif command -v dnf >/dev/null 2>&1; then
      if run_root dnf install -y nginx nginx-mod-stream; then
        NGINX_OK=1
      fi
    fi
    if [ "$NGINX_OK" -eq 0 ]; then
      warn "Could not install nginx with the stream module, so the front door was skipped. Install nginx (nginx-full on Debian/Ubuntu, nginx + nginx-mod-stream on Fedora) and run: sudo bash /opt/cleanplex/scripts/frontdoor/install.sh --user ${PLEX_USER}"
    fi
  fi

  if [ "$NGINX_OK" -eq 1 ]; then
    if ! run_root env PATH="/opt/homebrew/bin:/usr/local/bin:$PATH" bash "${BUNDLE_DIR}/scripts/frontdoor/install.sh" --user "$PLEX_USER"; then
      warn "Front door setup did not complete (make sure Plex is claimed first). Hayaa for Plex is still running without it."
    fi
  fi
fi

# ── Open browser & print final URL ────────────────────────────────────────────
if [ "$READY" -eq 1 ]; then
  if [ "$OS" = "Darwin" ]; then
    open "http://localhost:${PORT}" >/dev/null 2>&1 || true
  else
    if [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
      if command -v xdg-open >/dev/null 2>&1; then
        if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
          sudo -u "$SUDO_USER" DISPLAY="${DISPLAY:-}" WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-}" xdg-open "http://localhost:${PORT}" >/dev/null 2>&1 || true
        else
          xdg-open "http://localhost:${PORT}" >/dev/null 2>&1 || true
        fi
      fi
    fi
  fi
fi

DISPLAY_HOST="localhost"
if [ "$ALLOW_NETWORK" -eq 1 ]; then
  LAN_IP=""
  if [ "$OS" = "Darwin" ]; then
    LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
  else
    LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
    if [ -z "$LAN_IP" ] && command -v ip >/dev/null 2>&1; then
      LAN_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") {print $(i+1); exit}}' || true)"
    fi
  fi
  if [ -n "$LAN_IP" ]; then
    DISPLAY_HOST="$LAN_IP"
  fi
fi

echo ""
if [ "$READY" -eq 1 ]; then
  printf '%s%sHayaa for Plex is running. Open http://%s:%s to finish setup.%s\n' "$C_GREEN" "$C_BOLD" "$DISPLAY_HOST" "$PORT" "$C_RESET"
else
  warn "Hayaa for Plex is still starting up. Open http://${DISPLAY_HOST}:${PORT} in a moment to finish setup."
fi
