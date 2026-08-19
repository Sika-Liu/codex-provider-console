# Codex Provider Console

This console is designed to run on any Linux cloud server that has Docker and
Docker Compose. It only manages the Codex files mounted into the container;
each server keeps its own providers, credentials, backups, and audit log.

## Install Docker (Ubuntu/Debian)

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-plugin git
sudo systemctl enable --now docker
sudo docker version
sudo docker compose version
```

Optional: allow the current user to run Docker without `sudo`:

```bash
sudo usermod -aG docker "$USER"
```

Log out of SSH and sign in again after changing the group.

## Quick start

```bash
git clone https://github.com/Sika-Liu/codex-provider-console.git
cd codex-provider-console
bash install.sh
```

The installer creates `.env` from `.env.example`, defaults `CODEX_HOME_HOST` to
the current user's `~/.codex`, and starts the service. It does not overwrite
an existing Codex directory value unless `--force` is provided. The port and
bind address selected interactively are always applied.

In an interactive SSH session, the installer asks for the panel port and keeps
the service bound to localhost. It prints the internal address, SSH tunnel
command, configuration path, and a reminder to configure the reverse proxy in
the panel itself.
It also generates a panel administrator password and session secret. The
credentials are stored in `.env`, which the installer restricts to mode `600`.

To allow Docker installation through the installer on Ubuntu/Debian:

```bash
bash install.sh --install-docker
```

Useful installer options:

```bash
bash install.sh --codex-home /home/alice/.codex --port 8787
bash install.sh --force
```

The panel login is enabled by default. When using HTTPS through a reverse proxy,
set `PANEL_COOKIE_SECURE=true` in `.env` and restart the service:

```bash
bash codex-panel restart
```

By default the service listens only on `127.0.0.1:8787`. Access it locally with
an SSH tunnel:

```bash
ssh -L 8787:127.0.0.1:8787 <user>@<server>
```

Then open `http://127.0.0.1:8787` in a browser. Configure public access from
the panel's 反向代理 page; keep the upstream pointed at `127.0.0.1:8787` and
put it behind authenticated HTTPS because this panel can read and replace Codex
credentials.

## Data and portability

All state is stored under `CODEX_HOME_HOST`, including provider profiles,
backups, model catalogs, and the audit log. To move the console to another
server, copy that directory securely and point the new `.env` at the copy.

Do not commit `.env`, `auth.json`, provider profiles, or backup directories.

## Management

Run these commands from the repository directory:

```bash
bash codex-panel status
bash codex-panel logs
bash codex-panel restart
bash codex-panel update
```
