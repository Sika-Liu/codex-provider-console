#!/usr/bin/env bash
set -Eeuo pipefail

REPOSITORY_URL="https://github.com/Sika-Liu/codex-provider-console.git"
PROJECT_DIR="${HOME}/codex-provider-console"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This installer supports Linux servers only." >&2
  exit 1
fi

if ! command -v git >/dev/null 2>&1; then
  cat >&2 <<'EOF'
Git is required for one-command installation but was not found.
Install Git with your system package manager, then run this command again.
EOF
  exit 1
fi

if [[ -e "$PROJECT_DIR" ]]; then
  cat >&2 <<EOF
${PROJECT_DIR} already exists.
To protect its existing configuration and data, one-command installation will not overwrite it.
Use the existing directory:
  cd "${PROJECT_DIR}"
  bash install.sh
EOF
  exit 1
fi

git clone "$REPOSITORY_URL" "$PROJECT_DIR"
exec bash "$PROJECT_DIR/install.sh" "$@"
