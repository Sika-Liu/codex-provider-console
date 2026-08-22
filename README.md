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

该命令会克隆项目到当前用户的 `~/codex-provider-console`，然后自动运行安装脚本。安装脚本会检查 Docker、创建 `.env`、使用当前用户的 `~/.codex` 目录，并启动服务。默认监听 `0.0.0.0:8787`，便于直接通过服务器公网 IP 访问。

如果服务器没有 `curl` 但有 `wget`，执行：

```bash
bash <(wget -qO- https://raw.githubusercontent.com/Sika-Liu/codex-provider-console/main/bootstrap.sh)
```

引导脚本发现缺少 `curl` 或 `git` 时会询问是否安装，例如 `curl is not installed. Install curl now? [y/N]:`。若 `curl` 和 `wget` 都不存在，无法从网络下载引导脚本，需先通过系统包管理器安装其中任一个下载工具。

安装时会交互式询问控制台端口。服务默认监听 `0.0.0.0`，完成后会输出公网地址、内网地址、SSH 隧道命令、配置文件路径和安全组提示，行为与 1Panel 类似。
首次安装还会生成管理员用户名、随机密码和会话密钥，并在结果中显示一次。登录后可从左侧菜单退出登录。

登录控制台后打开左侧“健康检查”，可检查 Codex 数据目录、写入权限、`config.toml`、`auth.json`、控制台认证、当前供应商、供应商真实请求和磁盘空间。

缺少 Docker 时，先安装 Docker，或明确允许脚本自动安装：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Sika-Liu/codex-provider-console/main/bootstrap.sh) --install-docker
```

交互式一键安装时，如果检测不到 Docker，脚本会询问是否安装；输入 `y` 后会继续完成部署。Debian/Ubuntu 会使用系统包管理器，CentOS/RHEL 系列会使用 Docker 官方安装脚本。脚本不会自动执行可能触发系统重启的系统升级。

缺少 Codex CLI 时，安装脚本会询问是否安装。确认后会以非交互方式运行官方安装器，安装完成后自动继续部署面板，不会启动 Codex CLI 或要求额外执行命令。也可以直接执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Sika-Liu/codex-provider-console/main/bootstrap.sh) --install-codex
```

该选项要求服务器已安装 `curl`。官方安装脚本会将 Codex CLI 安装到服务器。

### 在控制台完成官方登录

选择“官方登录”供应商后，点击“开始官方登录”。控制台会显示 OpenAI 登录网址和一次性设备码；在自己的浏览器完成登录后，认证会安全写入服务器的 Codex 目录。点击“刷新令牌（重新登录）”会重新发起该流程。页面不会显示 `auth.json` 或任何登录令牌。

### 切换供应商后的重启

成功切换供应商后，页面右上角“重启 Codex”按钮才会变为可用。点击后会重启由控制台托管的 Codex 登录/运行服务，使之后从控制台发起的 Codex 会话读取新配置；执行完成后按钮会再次禁用。它不会终止 SSH 终端中手动运行的 Codex 对话或任务，避免中断正在进行的工作；这类终端会话请在任务完成后自行退出并重新启动。

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
