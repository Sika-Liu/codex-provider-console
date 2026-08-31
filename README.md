# Codex Provider Console

通用的 Codex 供应商控制台，可部署到安装了 Docker 的 Linux 云服务器（Ubuntu、Debian、CentOS、RHEL、Rocky、AlmaLinux 等）。

## 快速部署

### 部署前准备

请先手动更新服务器系统。Debian/Ubuntu 使用：

```bash
sudo apt update
sudo apt upgrade -y
```

CentOS/RHEL/Rocky/AlmaLinux 使用：

```bash
sudo dnf upgrade -y    # CentOS 7 可使用 yum update -y
```

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Sika-Liu/codex-provider-console/main/bootstrap.sh)
```

请使用 Codex Desktop SSH 连接所用的非 root 用户执行该命令，不要在 root 会话中部署。脚本会克隆项目到当前用户的 `~/codex-provider-console`，然后自动运行安装脚本；需要管理员权限时会请求 `sudo`。项目目录、Codex CLI、`~/.codex` 目录和面板容器运行身份会统一使用当前账户。默认监听 `0.0.0.0:8787`，便于直接通过服务器公网 IP 访问。

如果服务器没有 `curl` 但有 `wget`，执行：

```bash
bash <(wget -qO- https://raw.githubusercontent.com/Sika-Liu/codex-provider-console/main/bootstrap.sh)
```

引导脚本发现缺少 `curl` 或 `git` 时会询问是否安装，例如 `curl is not installed. Install curl now? [y/N]:`。若 `curl` 和 `wget` 都不存在，无法从网络下载引导脚本，需先通过系统包管理器安装其中任一个下载工具。

安装时会交互式询问控制台端口。服务默认监听 `0.0.0.0`，完成后会输出公网地址、内网地址、SSH 隧道命令、配置文件路径和安全组提示，行为与 1Panel 类似。

首次安装还会生成管理员用户名、随机密码和会话密钥，并在结果中显示一次。登录后可从左侧菜单退出登录。

### 部署中断后继续安装

部署过程中可以随时按 `Ctrl+C` 中断，不会删除已有项目文件。处理方式取决于中断时机：

- 在首次 `Panel port [8787]:` 提示前或此处中断时，项目仅完成克隆，尚未生成 `.env`。再次执行同一条一键部署命令即可自动续装：

  ```bash
  bash <(curl -fsSL https://raw.githubusercontent.com/Sika-Liu/codex-provider-console/main/bootstrap.sh)
  ```

- 已生成 `.env`、正在安装 Docker/CLI，或已开始构建容器后中断时，为保护已有配置，一键引导不会覆盖项目。请进入项目目录续装：

  ```bash
  cd ~/codex-provider-console
  codex-panel update
  ```

  `codex-panel update` 会拉取最新代码并重建已有容器，能够复用正在使用的面板端口。若已手动执行过 `git pull --ff-only`，可使用 `codex-panel restart` 仅重建并重启面板。

再次看到 `Panel port [8787]:` 时，直接按 Enter 使用默认端口，或输入未被占用的端口后按 Enter。

登录控制台后打开左侧“健康检查”，可检查 Codex 数据目录、写入权限、Codex CLI、SSH 登录用户与部署用户是否一致、`config.toml`、`auth.json`、控制台认证、当前供应商、供应商真实请求和磁盘空间。CLI 检查会实际执行通过 `/usr/local/bin/codex` 可访问的 CLI；这是 Codex Desktop 非交互 SSH 使用的路径，因此面板显示“通过”即表示桌面端可使用同一条路径。SSH 私钥不会上传或读取；请在“SSH 连接”中保存 Codex Desktop 使用的登录用户名。若未安装 Codex CLI，可直接在健康检查页选择安装；安装器会在部署用户的主目录中写入 CLI 和必要的 shell 配置。

健康检查还会验证面板管理的 Codex Desktop 部署密钥。没有密钥时，可在面板生成 Ed25519 密钥、将公钥授权到部署用户的 `authorized_keys`，并下载私钥。在 Codex App 的 SSH 连接中选择下载的私钥文件；私钥不会显示在页面中，但已登录面板可重复下载，请勿分享给他人。已有密钥不会被覆盖。

首次通过 Codex App 连接一台新服务器前，请先使用 Windows OpenSSH 手动连接一次并核对服务器指纹：

```powershell
ssh -i "$env:USERPROFILE\.ssh\<部署密钥文件>" <部署用户>@<服务器IP>
```

确认指纹属于该服务器后输入 `yes`，Windows 会将其写入 `~/.ssh/known_hosts`，随后 Codex App 才能完成主机身份校验。若同一 IP 重装了服务器，先执行 `ssh-keygen -R <服务器IP>`，再重新连接并确认新指纹。此校验与用于登录的 RSA 或 Ed25519 用户私钥无关：用户私钥用于证明“你是谁”，主机指纹用于证明“服务器是谁”。

缺少 Docker 时，先安装 Docker，或明确允许脚本自动安装：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Sika-Liu/codex-provider-console/main/bootstrap.sh) --install-docker
```

交互式一键安装时，如果检测不到 Docker，脚本会询问是否安装；输入 `y` 后会继续完成部署。Debian/Ubuntu 会使用系统包管理器，CentOS/RHEL 系列会使用 Docker 官方安装脚本。脚本不会自动执行可能触发系统重启的系统升级。

首次部署只安装控制台，不会询问或下载 Codex CLI。完成部署后，登录控制台并打开“健康检查”，在“Codex CLI”项中选择“安装 Codex CLI”。CLI 会安装到当前登录的非 root 运维用户，例如 `ubuntu`、`debian`、`ec2-user` 或自建账户。部署过程会准备 `/usr/local/bin/codex` 链接，使后续由健康检查安装的 CLI 可被 Codex Desktop 的非交互 SSH 检测到；此准备步骤可能请求一次 `sudo`，但不会下载或安装 CLI。

已有 root 部署如需切换为非 root 运维用户，请先卸载面板，再按本节重新部署；新项目会直接创建在目标用户的家目录中。登录令牌不会迁移，重新部署后请在控制台重新进行官方登录。

### 在控制台完成官方登录

选择“官方登录”供应商后，点击“开始官方登录”。控制台会显示 OpenAI 登录网址和一次性设备码；在自己的浏览器完成登录后，认证会安全写入服务器的 Codex 目录。点击“刷新令牌（重新登录）”会重新发起该流程。页面不会显示 `auth.json` 或任何登录令牌。

### 切换供应商后应用配置

成功切换供应商后，页面右上角“应用配置”按钮才会变为可用。控制台不会终止 SSH 终端中手动运行的 Codex 对话或任务；点击该按钮会重置控制台自身的登录会话，并确认后续由控制台发起的 Codex 会话读取新配置。已在 SSH 终端中运行的 Codex 请在任务完成后自行退出并重新启动。

自定义 Codex 目录或端口：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Sika-Liu/codex-provider-console/main/bootstrap.sh) --codex-home /home/ubuntu/.codex --port 8787
```

反向代理不是部署必选项。直接使用公网 IP 访问即可；如需域名和 HTTPS，登录控制台后打开左侧“反向代理”，填写域名、上游地址和证书路径。通过 HTTPS 反向代理访问时，将 `.env` 中的 `PANEL_COOKIE_SECURE` 改为 `true`，然后执行：

```bash
codex-panel restart
```

通过 SSH 隧道访问：

```bash
ssh -N -L 8787:127.0.0.1:8787 用户名@服务器IP
```

然后打开 `http://127.0.0.1:8787`。

## 日常管理

```bash
codex-panel status
codex-panel logs
codex-panel restart
codex-panel update
codex-panel help
codex-panel uninstall
```

安装完成后会创建 `codex-panel` 命令，可在任意目录使用。执行 `uninstall` 后，先确认移除面板容器、项目目录、`.env`、本项目 Docker 镜像和该命令；随后会询问是否删除 Codex CLI 与 Codex 数据，默认 `N`。卸载脚本绝不会卸载 Docker Engine 或删除其他 Docker 服务的数据。

### 卸载后确认

在服务器上执行以下命令：

```bash
test ! -e "$HOME/codex-provider-console" && echo "项目目录已删除"
test ! -e "$HOME/.codex" && echo "Codex 数据已删除"
```

第一条提示出现即表示项目文件已清除。仅在卸载时选择删除 Codex 后，第二条才应出现。若卸载时保留了 Docker，可再检查是否有项目容器、网络或镜像残留：

```bash
docker ps -a
docker network ls | grep codex-provider-console
docker images | grep codex-provider-console
```

若 `which docker` 没有任何输出，表示 Docker 未安装；因此不会存在该项目的 Docker 容器、网络或镜像残留。

完整说明请查看 [`DEPLOYMENT.md`](DEPLOYMENT.md)。公网访问时，请放在带 HTTPS 和登录认证的 Nginx/Caddy 反向代理后面。
