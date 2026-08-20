#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ENV_FILE="$PROJECT_DIR/.env"
CREATED_ENV=false
CODEX_HOME_HOST="${HOME}/.codex"
PANEL_BIND="0.0.0.0"
PANEL_PORT="8787"
PORT_SET=false
BIND_SET=false
INSTALL_DOCKER=false
INSTALL_CODEX=false
FORCE=false
PANEL_USERNAME="admin"
PANEL_PASSWORD=""
PANEL_SESSION_SECRET=""
CODEX_CLI_VERSION=""

usage() {
  cat <<'EOF'
Usage: bash install.sh [options]

Options:
  --codex-home <path>   Host directory mounted as /codex (default: ~/.codex)
  --bind <address>      Advanced override (default: 0.0.0.0)
  --port <port>         Host port (default: 8787)
  --install-docker      Install Docker on Ubuntu/Debian when it is missing
  --install-codex       Install Codex CLI with the official installer when missing
  --force               Replace matching settings in an existing .env file
  -h, --help            Show this help

The default binds the panel to localhost. Use an SSH tunnel or an authenticated
HTTPS reverse proxy instead of exposing the panel directly to the internet.
EOF
}

require_value() {
  [[ -n "${2:-}" ]] || { echo "Missing value for $1" >&2; exit 1; }
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --codex-home) require_value "$1" "${2:-}"; CODEX_HOME_HOST="$2"; shift ;;
    --bind) require_value "$1" "${2:-}"; PANEL_BIND="$2"; BIND_SET=true; shift ;;
    --port) require_value "$1" "${2:-}"; PANEL_PORT="$2"; PORT_SET=true; shift ;;
    --install-docker) INSTALL_DOCKER=true ;;
    --install-codex) INSTALL_CODEX=true ;;
    --force) FORCE=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unsupported option: $1" >&2; usage; exit 1 ;;
  esac
  shift
done

[[ "$(uname -s)" == "Linux" ]] || { echo "This installer supports Linux servers only." >&2; exit 1; }
if [[ -f "$ENV_FILE" ]]; then
  existing_port=$(sed -n 's/^PANEL_PORT=//p' "$ENV_FILE" | tail -n 1)
  existing_bind=$(sed -n 's/^PANEL_BIND=//p' "$ENV_FILE" | tail -n 1)
  [[ "$PORT_SET" == true || -z "$existing_port" ]] || PANEL_PORT="$existing_port"
  [[ "$BIND_SET" == true || -z "$existing_bind" ]] || PANEL_BIND="$existing_bind"
fi

if [[ -t 0 && "$PORT_SET" == false ]]; then
  read -r -p "Panel port [${PANEL_PORT}]: " requested_port
  PANEL_PORT="${requested_port:-$PANEL_PORT}"
  PORT_SET=true
fi

[[ "$PANEL_PORT" =~ ^[1-9][0-9]{0,4}$ && "$PANEL_PORT" -le 65535 ]] || { echo "Invalid port: $PANEL_PORT" >&2; exit 1; }
[[ "$PANEL_BIND" == "127.0.0.1" || "$PANEL_BIND" == "0.0.0.0" || "$PANEL_BIND" == "::1" || "$PANEL_BIND" == "::" ]] || { echo "Unsupported bind address: $PANEL_BIND" >&2; exit 1; }

port_in_use() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltn | awk '{print $4}' | grep -Eq "[:.]${PANEL_PORT}$"
  elif command -v netstat >/dev/null 2>&1; then
    netstat -ltn | awk '{print $4}' | grep -Eq "[:.]${PANEL_PORT}$"
  else
    return 1
  fi
}

if port_in_use; then
  echo "Port ${PANEL_PORT} is already in use. Choose another port with --port." >&2
  exit 1
fi

install_docker() {
  [[ -r /etc/os-release ]] && . /etc/os-release || true
  case "${ID:-}" in
    ubuntu|debian)
      if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
        apt_cmd=(apt-get)
        systemctl_cmd=(systemctl)
      else
        apt_cmd=(sudo apt-get)
        systemctl_cmd=(sudo systemctl)
      fi
      echo "Installing Docker packages with apt."
      "${apt_cmd[@]}" update
      "${apt_cmd[@]}" install -y docker.io
      if ! docker compose version >/dev/null 2>&1; then
        "${apt_cmd[@]}" install -y docker-compose-v2 2>/dev/null || "${apt_cmd[@]}" install -y docker-compose-plugin
      fi
      "${systemctl_cmd[@]}" enable --now docker
      ;;
    *)
      echo "Automatic Docker installation is only supported on Ubuntu/Debian." >&2
      echo "Install Docker and the Docker Compose plugin, then run this script again." >&2
      exit 1
      ;;
  esac
}

random_hex() {
  od -An -N "$1" -tx1 /dev/urandom | tr -d ' \n'
}

if ! command -v docker >/dev/null 2>&1; then
  docker_choice="n"
  if [[ "$INSTALL_DOCKER" == true ]]; then
    docker_choice="y"
  elif [[ -t 0 ]]; then
    read -r -p "Docker is not installed. Install it now and continue deployment? [y/N]: " docker_choice
  fi
  case "${docker_choice:-n}" in
    y|Y|yes|YES) install_docker ;;
    *)
      cat >&2 <<'EOF'
Docker is required but was not found.
Run this installer again and confirm Docker installation, or use:
  bash install.sh --install-docker
EOF
      exit 1
      ;;
  esac
  if ! command -v docker >/dev/null 2>&1; then
    echo "Docker installation did not complete successfully." >&2
    exit 1
  fi
fi

docker compose version >/dev/null 2>&1 || {
  echo "Docker Compose v2 is required. Install the docker-compose-plugin package and retry." >&2
  exit 1
}

install_codex() {
  if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required to install Codex CLI. Install curl, then retry." >&2
    return 1
  fi
  echo "Installing Codex CLI with the official installer."
  curl -fsSL https://chatgpt.com/codex/install.sh | sh
}

if ! command -v codex >/dev/null 2>&1; then
  install_choice="n"
  if [[ "$INSTALL_CODEX" == true ]]; then
    install_choice="y"
  elif [[ -t 0 ]]; then
    read -r -p "Codex CLI was not found. Install it now with the official installer? [y/N]: " install_choice
  fi
  case "${install_choice:-n}" in
    y|Y|yes|YES) install_codex || exit 1 ;;
  esac
fi

CODEX_CLI_VERSION="$(codex --version 2>/dev/null | head -n 1 || true)"
CODEX_CLI_VERSION="${CODEX_CLI_VERSION:-not_installed}"

if ! docker info >/dev/null 2>&1; then
  cat >&2 <<'EOF'
Docker is installed but the current user cannot access it.
Run this once, sign out of SSH, sign in again, then retry:
  sudo usermod -aG docker "$USER"
EOF
  exit 1
fi

mkdir -p "$CODEX_HOME_HOST"

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$PROJECT_DIR/.env.example" "$ENV_FILE"
  CREATED_ENV=true
fi

set_env() {
  local key="$1"
  local value="$2"
  local replace="${3:-false}"
  if grep -q "^${key}=" "$ENV_FILE"; then
    if [[ "$FORCE" == true || "$CREATED_ENV" == true || "$replace" == true ]]; then
      sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    fi
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

set_env CODEX_HOME_HOST "$CODEX_HOME_HOST"
set_env PANEL_BIND "$PANEL_BIND" "$BIND_SET"
set_env PANEL_PORT "$PANEL_PORT" "$PORT_SET"
set_env PUID "$(id -u)"
set_env PGID "$(id -g)"
set_env CODEX_CLI_VERSION "$CODEX_CLI_VERSION" true

existing_username=$(sed -n 's/^PANEL_USERNAME=//p' "$ENV_FILE" | tail -n 1)
existing_password=$(sed -n 's/^PANEL_PASSWORD=//p' "$ENV_FILE" | tail -n 1)
existing_secret=$(sed -n 's/^PANEL_SESSION_SECRET=//p' "$ENV_FILE" | tail -n 1)
PANEL_USERNAME="${existing_username:-$PANEL_USERNAME}"
if [[ -z "$existing_password" || "$existing_password" == "change-this-password" ]]; then
  PANEL_PASSWORD=$(random_hex 18)
else
  PANEL_PASSWORD="$existing_password"
fi
if [[ -z "$existing_secret" || "$existing_secret" == "change-this-session-secret" ]]; then
  PANEL_SESSION_SECRET=$(random_hex 32)
else
  PANEL_SESSION_SECRET="$existing_secret"
fi
set_env PANEL_AUTH_ENABLED "true" true
set_env PANEL_USERNAME "$PANEL_USERNAME"
set_env PANEL_PASSWORD "$PANEL_PASSWORD"
set_env PANEL_SESSION_SECRET "$PANEL_SESSION_SECRET"
set_env PANEL_COOKIE_SECURE "false"
chmod 600 "$ENV_FILE"

docker compose -f "$PROJECT_DIR/compose.yml" up -d --build

local_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
local_ip=${local_ip:-127.0.0.1}
public_ip=$(curl -4fsS --connect-timeout 3 --max-time 5 https://api.ipify.org 2>/dev/null || true)
public_ip=${public_ip:-N/A}

if [[ "$PANEL_BIND" == "0.0.0.0" || "$PANEL_BIND" == "::" ]]; then
  external_address="http://${public_ip}:${PANEL_PORT}"
  internal_address="http://${local_ip}:${PANEL_PORT}"
  exposure_note="Open TCP port ${PANEL_PORT} in the cloud security group and host firewall. Reverse proxy setup is optional and can be configured in the panel."
else
  external_address="Disabled (custom localhost binding)"
  internal_address="http://127.0.0.1:${PANEL_PORT}"
  exposure_note="The service uses a custom local bind. Reverse proxy setup is optional and can be configured in the panel."
fi

cat <<EOF

===============================================================
Codex Provider Console installed successfully
===============================================================
External address: ${external_address}
Internal address: ${internal_address}
Listening address: ${PANEL_BIND}:${PANEL_PORT}
SSH tunnel: ssh -N -L ${PANEL_PORT}:127.0.0.1:${PANEL_PORT} $(whoami)@<server-ip>
Config file: ${ENV_FILE}
Codex data: ${CODEX_HOME_HOST}
Username: ${PANEL_USERNAME}
Password: ${PANEL_PASSWORD}
Codex CLI: ${CODEX_CLI_VERSION}

${exposure_note}

Management:
  bash codex-panel status
  bash codex-panel logs
  bash codex-panel restart
  bash codex-panel update
  bash codex-panel help
  bash codex-panel uninstall
===============================================================
EOF
