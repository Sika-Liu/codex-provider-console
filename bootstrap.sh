#!/usr/bin/env bash
set -Eeuo pipefail

REPOSITORY_URL="https://github.com/Sika-Liu/codex-provider-console.git"
PROJECT_DIR="${HOME}/codex-provider-console"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This installer supports Linux servers only." >&2
  exit 1
fi

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
  cat >&2 <<EOF
${PROJECT_DIR} already exists.
To protect its existing configuration and data, one-command installation will not overwrite it.
EOF
  exit 1
fi

git clone "$REPOSITORY_URL" "$PROJECT_DIR"
exec bash "$PROJECT_DIR/install.sh" "$@"
