# Codex Provider Console

This console is designed to run on any Linux cloud server that has Docker and
Docker Compose. It only manages the Codex files mounted into the container;
each server keeps its own providers, credentials, backups, and audit log.

## Install Docker manually

Ubuntu/Debian：

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-plugin git
sudo systemctl enable --now docker
sudo docker version
sudo docker compose version
```

CentOS/RHEL/Rocky/AlmaLinux/Fedora：

```bash
curl -fsSL https://get.docker.com | sh
sudo systemctl enable --now docker
sudo docker version
sudo docker compose version
```

也可以直接让安装脚本在支持的发行版上安装 Docker：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Sika-Liu/codex-provider-console/main/bootstrap.sh) --install-docker
```

Optional: allow the current user to run Docker without `sudo`:

```bash
sudo usermod -aG docker "$USER"
```

Log out of SSH and sign in again after changing the group.

## Quick start

Before deployment, update the server manually. If a new kernel is installed,
reboot the server when prompted before continuing:

```bash
sudo apt update
sudo apt upgrade -y
```

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Sika-Liu/codex-provider-console/main/bootstrap.sh)
```

The one-command installer clones the project to `~/codex-provider-console` and
starts `install.sh` automatically and refuses to overwrite an existing project
directory or its data. When started with `wget` or a local copy, it asks whether
to install missing `curl` and `git` tools. If `curl` is not installed but `wget`
is available, use:

```bash
bash <(wget -qO- https://raw.githubusercontent.com/Sika-Liu/codex-provider-console/main/bootstrap.sh)
```

If neither `curl` nor `wget` is present, install one download tool first; a
network installer cannot run before a downloader is available.

The installer creates `.env` from `.env.example`, defaults `CODEX_HOME_HOST` to
the current user's `~/.codex`, and starts the service. It does not overwrite
an existing Codex directory value unless `--force` is provided. The port and
bind address selected interactively are always applied.

In an interactive SSH session, the installer asks for the panel port, defaults
to `0.0.0.0`, and prints the external address, internal address, SSH tunnel
command, configuration path, and cloud firewall reminder. Reverse proxy setup
is optional and is configured later from the panel.
It also generates a panel administrator password and session secret. The
credentials are stored in `.env`, which the installer restricts to mode `600`.

To allow Docker installation through the installer on common Linux distributions:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Sika-Liu/codex-provider-console/main/bootstrap.sh) --install-docker
```

When run interactively, the one-command installer asks whether to install Docker if it
is missing. Answer `y` to install it and continue deployment. Debian/Ubuntu use
the distribution packages; CentOS/RHEL/Rocky/AlmaLinux/Fedora use Docker's
official installer. The installer does not run a full system upgrade
automatically because that can upgrade the kernel and require a reboot.

If Codex CLI is missing, the installer asks whether to install it with the
official standalone installer. It runs the official installer non-interactively,
then continues panel deployment without launching an interactive Codex session. For
non-interactive setup, use:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Sika-Liu/codex-provider-console/main/bootstrap.sh) --install-codex
```

`curl` must already be available on the server.

Useful installer options:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Sika-Liu/codex-provider-console/main/bootstrap.sh) --codex-home /home/alice/.codex --port 8787
```

The panel login is enabled by default. When using HTTPS through a reverse proxy,
set `PANEL_COOKIE_SECURE=true` in `.env` and restart the service:

```bash
codex-panel restart
```

If you choose the localhost override, the service listens on `127.0.0.1:8787`.
Access it locally with an SSH tunnel:

```bash
ssh -L 8787:127.0.0.1:8787 <user>@<server>
```

For the default public binding, open `http://<server-public-ip>:8787` after
opening the port in the cloud security group. Alternatively, use the SSH tunnel
shown by the installer. Configure a domain and HTTPS from the panel's 反向代理
page only when needed; the panel can read and replace Codex credentials, so
public deployments must retain login authentication.

After signing in, use the left-side **健康检查** page before running Codex. It
checks the mounted Codex directory, write permissions, configuration and auth
files, panel authentication, the active provider, a real upstream request, and
available disk space.

## Data and portability

All state is stored under `CODEX_HOME_HOST`, including provider profiles,
backups, model catalogs, and the audit log. To move the console to another
server, copy that directory securely and point the new `.env` at the copy.

Do not commit `.env`, `auth.json`, provider profiles, or backup directories.

## Management

Run these commands from the repository directory:

```bash
codex-panel status
codex-panel logs
codex-panel restart
codex-panel update
codex-panel help
codex-panel uninstall
```

Installation creates the `codex-panel` command so it can be used from any directory. `uninstall` 会要求确认，然后停止并移除 Docker 容器，并删除项目目录、`.env` 以及 `CODEX_HOME_HOST` 指向的 Codex 数据目录，同时移除该命令。若 Docker 已被卸载，脚本会跳过容器清理，仍会删除项目和 Codex 数据。该操作不可恢复，请先备份需要保留的内容。

## Verify uninstall

Run the following on the server:

```bash
test ! -e "$HOME/codex-provider-console" && echo "Project directory removed"
test ! -e "$HOME/.codex" && echo "Codex data removed"
```

If both messages are printed, the project files and Codex data are removed. On
servers with Docker installed, check for remaining project resources:

```bash
docker ps -a
docker network ls | grep codex-provider-console
docker images | grep codex-provider-console
```

No output from `which docker` means Docker is not installed, so this project
cannot have Docker containers, networks, or images on that server.
