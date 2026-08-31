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
DEPLOY_USER="${SUDO_USER:-$(id -un)}"
CODEX_CLI_USER="$DEPLOY_USER"
CODEX_HOME_SET=false
DOCKER_GROUP_NOTE=""
CODEX_USER_HOME_HOST=""

usage() {
  cat <<'EOF'
Usage: bash install.sh [options]

Options:
  --codex-home <path>   Host directory mounted as /codex (default: ~/.codex)
  --bind <address>      Advanced override (default: 0.0.0.0)
  --port <port>         Host port (default: 8787)
  --install-docker      Install Docker when it is missing (common Linux distros)
  --install-codex       Install Codex CLI with the official installer when missing
  --force               Replace matching settings in an existing .env file
  -h, --help            Show this help

The default binds the panel to 0.0.0.0 for direct public-IP access. Protect the
panel with its administrator login, cloud firewall rules, or an HTTPS reverse proxy.
EOF
}

require_value() {
  [[ -n "${2:-}" ]] || { echo "Missing value for $1" >&2; exit 1; }
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --deploy-user|--codex-cli-user) require_value "$1" "${2:-}"; DEPLOY_USER="$2"; shift ;;
    --codex-home) require_value "$1" "${2:-}"; CODEX_HOME_HOST="$2"; CODEX_HOME_SET=true; shift ;;
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
if [[ "$(id -u)" -eq 0 && -z "${SUDO_USER:-}" ]]; then
  cat >&2 <<'EOF'
Please deploy the console from a non-root user account.
Sign in with the user that Codex Desktop will use for SSH, then run bash install.sh.
The installer will request sudo only when needed.
EOF
  exit 1
fi
if [[ "$(id -u)" -ne 0 && "$DEPLOY_USER" != "$(id -un)" ]]; then
  echo "A non-root deployment can only use the current user: $(id -un)." >&2
  exit 1
fi
id "$DEPLOY_USER" >/dev/null 2>&1 || { echo "Deployment user does not exist: $DEPLOY_USER" >&2; exit 1; }
[[ "$DEPLOY_USER" != "root" ]] || { echo "The deployment user must not be root." >&2; exit 1; }
CODEX_CLI_USER="$DEPLOY_USER"
CODEX_CLI_HOME=$(getent passwd "$DEPLOY_USER" | cut -d: -f6)
[[ -n "$CODEX_CLI_HOME" ]] || { echo "Could not determine home directory for $DEPLOY_USER." >&2; exit 1; }
CODEX_USER_HOME_HOST="$CODEX_CLI_HOME"
if [[ "$CODEX_HOME_SET" == false && ! -f "$ENV_FILE" ]]; then
  CODEX_HOME_HOST="$CODEX_CLI_HOME/.codex"
fi
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

existing_deployment_uses_port() {
  [[ -f "$ENV_FILE" ]] || return 1
  command -v docker >/dev/null 2>&1 || return 1
  docker compose -f "$PROJECT_DIR/compose.yml" ps -q 2>/dev/null | grep -q .
}

if port_in_use; then
  if existing_deployment_uses_port; then
    echo "Port ${PANEL_PORT} is used by this deployment; it will be recreated."
  else
    echo "Port ${PANEL_PORT} is already in use. Choose another port with --port." >&2
    exit 1
  fi
fi

install_docker() {
  [[ -r /etc/os-release ]] && . /etc/os-release || true
  local distro="${ID:-}"
  local like="${ID_LIKE:-}"
  case "$distro:$like" in
    ubuntu:*|debian:*|*:debian)
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
    centos:*|rhel:*|rocky:*|almalinux:*|fedora:*|ol:*|*:rhel|*:fedora)
      if ! command -v curl >/dev/null 2>&1; then
        if command -v dnf >/dev/null 2>&1; then
          dnf_cmd=(dnf)
        elif command -v yum >/dev/null 2>&1; then
          dnf_cmd=(yum)
        else
          echo "Neither dnf nor yum is available; install Docker manually." >&2
          exit 1
        fi
        if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
          "${dnf_cmd[@]}" install -y curl
        else
          sudo "${dnf_cmd[@]}" install -y curl
        fi
      fi
      echo "Installing Docker with the official Docker installer for ${distro:-RHEL-like}."
      curl -fsSL https://get.docker.com | sh
      if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
        systemctl enable --now docker
      else
        sudo systemctl enable --now docker
      fi
      ;;
    *)
      echo "Automatic Docker installation is not configured for ${distro:-this distribution}." >&2
      echo "Install Docker Engine and the Docker Compose v2 plugin, then run this script again." >&2
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
  echo "Installing Codex CLI for ${CODEX_CLI_USER} with the official installer."
  if [[ "$(id -un)" == "$CODEX_CLI_USER" ]]; then
    curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=true sh
  else
    runuser -l "$CODEX_CLI_USER" -c 'curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=true sh'
  fi
}

expose_codex_to_ssh() {
  local codex_bin="$CODEX_CLI_HOME/.local/bin/codex"
  local system_bin="/usr/local/bin/codex"

  [[ -x "$codex_bin" ]] || return 0

  # Codex Desktop probes the CLI through a non-interactive SSH command. That
  # command does not load the user's shell profile, so ~/.local/bin is absent.
  if [[ -e "$system_bin" && ! -L "$system_bin" ]]; then
    echo "Codex CLI is installed for ${CODEX_CLI_USER}, but ${system_bin} is a regular file; leaving it unchanged." >&2
    echo "To expose this user's CLI to Codex Desktop, point ${system_bin} to ${codex_bin}." >&2
    return 0
  fi

  if [[ -w "$(dirname "$system_bin")" ]]; then
    ln -sfn "$codex_bin" "$system_bin"
  elif command -v sudo >/dev/null 2>&1; then
    sudo ln -sfn "$codex_bin" "$system_bin"
  else
    echo "Codex CLI is installed for ${CODEX_CLI_USER}, but sudo is required to expose it at ${system_bin}." >&2
    return 0
  fi

  echo "Exposed Codex CLI for non-interactive SSH: ${system_bin} -> ${codex_bin}"
}

if [[ "$(id -u)" -eq 0 ]] && getent group docker >/dev/null 2>&1 && ! id -nG "$DEPLOY_USER" | tr ' ' '\n' | grep -qx docker; then
  usermod -aG docker "$DEPLOY_USER"
  DOCKER_GROUP_NOTE="The operational user was added to the docker group. Sign out and sign in again before using codex-panel."
fi

codex_version_for_user() {
  local codex_bin="$CODEX_CLI_HOME/.local/bin/codex"
  local version=""
  if [[ -x "$codex_bin" ]]; then
    if [[ "$(id -un)" == "$CODEX_CLI_USER" ]]; then
      version=$("$codex_bin" --version 2>/dev/null | head -n 1 || true)
    else
      version=$(runuser -u "$CODEX_CLI_USER" -- "$codex_bin" --version 2>/dev/null | head -n 1 || true)
    fi
  fi
  printf '%s\n' "$version"
}

if [[ -z "$(codex_version_for_user)" ]]; then
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

CODEX_CLI_VERSION="$(codex_version_for_user)"
CODEX_CLI_VERSION="${CODEX_CLI_VERSION:-not_installed}"
expose_codex_to_ssh

if ! docker info >/dev/null 2>&1; then
  cat >&2 <<'EOF'
Docker is installed but the current user cannot access it.
Run this once, sign out of SSH, sign in again, then retry:
  sudo usermod -aG docker "$USER"
EOF
  exit 1
fi

mkdir -p "$CODEX_HOME_HOST"
if [[ "$(id -u)" -eq 0 ]]; then
  chown "$DEPLOY_USER":"$(id -gn "$DEPLOY_USER")" "$CODEX_HOME_HOST"
fi

install_management_command() {
  local command_dir="$CODEX_CLI_HOME/.local/bin"
  local command_path="$command_dir/codex-panel"
  local profile="$CODEX_CLI_HOME/.bashrc"

  mkdir -p "$command_dir"
  printf '#!/usr/bin/env bash\nexec bash %q "$@"\n' "$PROJECT_DIR/codex-panel" > "$command_path"
  chmod 755 "$command_path"
  export PATH="$command_dir:$PATH"
  if ! grep -Fqx 'export PATH="$HOME/.local/bin:$PATH"' "$profile" 2>/dev/null; then
    printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$profile"
  fi
  if [[ "$(id -u)" -eq 0 ]]; then
    chown "$DEPLOY_USER":"$(id -gn "$DEPLOY_USER")" "$command_dir" "$command_path" "$profile"
  fi
}

install_management_command

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$PROJECT_DIR/.env.example" "$ENV_FILE"
  CREATED_ENV=true
fi

PANEL_PUID=$(id -u "$DEPLOY_USER")
PANEL_PGID=$(id -g "$DEPLOY_USER")

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
set_env PUID "$PANEL_PUID" "$FORCE"
set_env PGID "$PANEL_PGID" "$FORCE"
set_env CODEX_CLI_VERSION "$CODEX_CLI_VERSION" true
set_env CODEX_CLI_USER "$CODEX_CLI_USER" true
set_env DEPLOY_USER "$DEPLOY_USER" true
set_env CODEX_USER_HOME_HOST "$CODEX_USER_HOME_HOST" true

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
if [[ "$(id -u)" -eq 0 ]]; then
  chown -R "$DEPLOY_USER":"$(id -gn "$DEPLOY_USER")" "$PROJECT_DIR"
fi

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
SSH tunnel: ssh -N -L ${PANEL_PORT}:127.0.0.1:${PANEL_PORT} ${DEPLOY_USER}@<server-ip>
Config file: ${ENV_FILE}
Operational user: ${DEPLOY_USER}
Codex data: ${CODEX_HOME_HOST}
Codex CLI user: ${CODEX_CLI_USER}
Management command: codex-panel
Username: ${PANEL_USERNAME}
Password: ${PANEL_PASSWORD}
Codex CLI: ${CODEX_CLI_VERSION}

${DOCKER_GROUP_NOTE}

${exposure_note}

Management:
  codex-panel status
  codex-panel logs
  codex-panel restart
  codex-panel update
  codex-panel help
  codex-panel uninstall
===============================================================
EOF
