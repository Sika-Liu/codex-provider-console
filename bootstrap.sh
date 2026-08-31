#!/usr/bin/env bash
set -Eeuo pipefail

REPOSITORY_URL="https://github.com/Sika-Liu/codex-provider-console.git"
DEPLOY_USER="${SUDO_USER:-$(id -un)}"
INSTALL_ARGS=()

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This installer supports Linux servers only." >&2
  exit 1
fi

require_value() {
  [[ -n "${2:-}" ]] || { echo "Missing value for $1" >&2; exit 1; }
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    *) INSTALL_ARGS+=("$1") ;;
  esac
  shift
done

if [[ "$(id -u)" -eq 0 ]]; then
  cat >&2 <<'EOF'
Please deploy the console from a non-root user account.
Exit this root session, sign in with the user that Codex Desktop will use for SSH,
then run this command again. The installer will request sudo only when needed.
EOF
  exit 1
fi

DEPLOY_USER=$(id -un)
DEPLOY_HOME=$(getent passwd "$DEPLOY_USER" | cut -d: -f6)
[[ -n "$DEPLOY_HOME" ]] || { echo "Could not determine home directory for $DEPLOY_USER." >&2; exit 1; }
PROJECT_DIR="${DEPLOY_HOME}/codex-provider-console"

run_privileged() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    echo "Administrator privileges are required to install packages." >&2
    exit 1
  fi
}

install_package() {
  local package="$1"
  [[ -r /etc/os-release ]] && . /etc/os-release || true

  case "${ID:-}" in
    ubuntu|debian)
      run_privileged apt-get update
      run_privileged apt-get install -y "$package"
      ;;
    centos|rhel|rocky|almalinux|fedora|ol)
      if command -v dnf >/dev/null 2>&1; then
        run_privileged dnf install -y "$package"
      else
        run_privileged yum install -y "$package"
      fi
      ;;
    *)
      echo "Automatic package installation is not supported on ${ID:-this distribution}." >&2
      exit 1
      ;;
  esac
}

ensure_command() {
  local command="$1"
  local package="$2"
  local answer="n"

  command -v "$command" >/dev/null 2>&1 && return 0
  if [[ -t 0 ]]; then
    read -r -p "${command} is not installed. Install ${command} now? [y/N]: " answer
  fi
  case "${answer:-n}" in
    y|Y|yes|YES) install_package "$package" ;;
    *)
      echo "${command} is required for one-command installation." >&2
      exit 1
      ;;
  esac
}

# When launched with wget or from a local copy, bootstrap can also install curl.
ensure_command curl curl
ensure_command git git

if [[ -e "$PROJECT_DIR" ]]; then
  if [[ -d "$PROJECT_DIR/.git" && -f "$PROJECT_DIR/install.sh" && ! -e "$PROJECT_DIR/.env" ]]; then
    echo "An incomplete deployment was found at ${PROJECT_DIR}; resuming it."
    git -C "$PROJECT_DIR" pull --ff-only
  else
    cat >&2 <<EOF
${PROJECT_DIR} already exists.
To protect its existing configuration and data, one-command installation will not overwrite it.
EOF
    exit 1
  fi
else
  if [[ "$(id -u)" -eq 0 ]]; then
    runuser -u "$DEPLOY_USER" -- git clone "$REPOSITORY_URL" "$PROJECT_DIR"
  else
    git clone "$REPOSITORY_URL" "$PROJECT_DIR"
  fi
fi
if [[ "$(id -u)" -eq 0 ]]; then
  exec bash "$PROJECT_DIR/install.sh" --deploy-user "$DEPLOY_USER" "${INSTALL_ARGS[@]}"
elif command -v sudo >/dev/null 2>&1; then
  exec sudo bash "$PROJECT_DIR/install.sh" --deploy-user "$DEPLOY_USER" "${INSTALL_ARGS[@]}"
else
  exec bash "$PROJECT_DIR/install.sh" --deploy-user "$DEPLOY_USER" "${INSTALL_ARGS[@]}"
fi
