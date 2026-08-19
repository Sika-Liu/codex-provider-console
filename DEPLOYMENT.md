# Codex Provider Console

This console is designed to run on any Linux cloud server that has Docker and
Docker Compose. It only manages the Codex files mounted into the container;
each server keeps its own providers, credentials, backups, and audit log.

## Quick start

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

```bash
git clone <your-repository-url> codex-provider-console
cd codex-provider-console
cp .env.example .env
```

Edit `.env` and set `CODEX_HOME_HOST` to the server user's Codex directory.
The directory must contain (or be able to create) `config.toml` and may contain
`auth.json`. Set `PUID` and `PGID` to the owner of that directory:

```bash
id -u
id -g
```

Start the service:

```bash
docker compose up -d --build
docker compose logs -f codex-provider-console
```

By default the service listens only on `127.0.0.1:8787`. Access it locally with
an SSH tunnel:

```bash
ssh -L 8787:127.0.0.1:8787 <user>@<server>
```

Then open `http://127.0.0.1:8787` in a browser. For public access, put it
behind an authenticated HTTPS reverse proxy and change `PANEL_BIND` only when
you understand the exposure: this panel can read and replace Codex credentials.

## Data and portability

All state is stored under `CODEX_HOME_HOST`, including provider profiles,
backups, model catalogs, and the audit log. To move the console to another
server, copy that directory securely and point the new `.env` at the copy.

Do not commit `.env`, `auth.json`, provider profiles, or backup directories.
