#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ENV_FILE="$PROJECT_DIR/.env"
CREATED_ENV=false
CODEX_HOME_HOST="${HOME}/.codex"
PANEL_BIND="127.0.0.1"
PANEL_PORT="8787"
INSTALL_DOCKER=false
FORCE=false

usage() {
  cat <<'EOF'
Usage: bash install.sh [options]

Options:
  --codex-home <path>   Host directory mounted as /codex (default: ~/.codex)
  --bind <address>      Bind address (default: 127.0.0.1)
  --port <port>         Host port (default: 8787)
  --install-docker      Install Docker on Ubuntu/Debian when it is missing
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
    --bind) require_value "$1" "${2:-}"; PANEL_BIND="$2"; shift ;;
    --port) require_value "$1" "${2:-}"; PANEL_PORT="$2"; shift ;;
    --install-docker) INSTALL_DOCKER=true ;;
    --force) FORCE=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unsupported option: $1" >&2; usage; exit 1 ;;
  esac
  shift
done

[[ "$(uname -s)" == "Linux" ]] || { echo "This installer supports Linux servers only." >&2; exit 1; }
[[ "$PANEL_PORT" =~ ^[1-9][0-9]{0,4}$ && "$PANEL_PORT" -le 65535 ]] || { echo "Invalid port: $PANEL_PORT" >&2; exit 1; }

install_docker() {
  [[ -r /etc/os-release ]] && . /etc/os-release || true
  case "${ID:-}" in
    ubuntu|debian)
      echo "Installing Docker packages with apt."
      sudo apt update
      sudo apt install -y docker.io docker-compose-plugin
      sudo systemctl enable --now docker
      ;;
    *)
      echo "Automatic Docker installation is only supported on Ubuntu/Debian." >&2
      echo "Install Docker and the Docker Compose plugin, then run this script again." >&2
      exit 1
      ;;
  esac
}

if ! command -v docker >/dev/null 2>&1; then
  if [[ "$INSTALL_DOCKER" == true ]]; then
    install_docker
  else
    cat >&2 <<'EOF'
Docker is required but was not found.
Install Docker first, or explicitly allow this installer to install it:
  bash install.sh --install-docker
EOF
    exit 1
  fi
fi

docker compose version >/dev/null 2>&1 || {
  echo "Docker Compose v2 is required. Install the docker-compose-plugin package and retry." >&2
  exit 1
}

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
  if grep -q "^${key}=" "$ENV_FILE"; then
    if [[ "$FORCE" == true || "$CREATED_ENV" == true ]]; then
      sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    fi
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

set_env CODEX_HOME_HOST "$CODEX_HOME_HOST"
set_env PANEL_BIND "$PANEL_BIND"
set_env PANEL_PORT "$PANEL_PORT"
set_env PUID "$(id -u)"
set_env PGID "$(id -g)"

docker compose -f "$PROJECT_DIR/compose.yml" up -d --build

cat <<EOF

Codex Provider Console is running.

Server access:  http://${PANEL_BIND}:${PANEL_PORT}
SSH tunnel:     ssh -N -L ${PANEL_PORT}:127.0.0.1:${PANEL_PORT} $(whoami)@<server-ip>
Local browser:  http://127.0.0.1:${PANEL_PORT}

Management commands:
  bash codex-panel status
  bash codex-panel logs
  bash codex-panel update
EOF
