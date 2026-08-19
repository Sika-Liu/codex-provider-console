# Codex Provider Console

通用的 Codex 供应商控制台，可部署到任意安装了 Docker 的 Linux 云服务器。

## 快速部署

```bash
git clone https://github.com/Sika-Liu/codex-provider-console.git
cd codex-provider-console
bash install.sh
```

安装脚本会检查 Docker、创建 `.env`、使用当前用户的 `~/.codex` 目录，并启动服务。默认只监听 `127.0.0.1:8787`，不会直接开放公网端口。

安装时会交互式询问：

- 控制台端口；
- 是否公开监听到公网。

完成后会输出公网地址、内网地址、SSH 隧道命令、配置文件路径和安全组提示。
首次安装还会生成管理员用户名、随机密码和会话密钥，并在结果中显示一次。登录后可从左侧菜单退出登录。

缺少 Docker 时，先安装 Docker，或明确允许脚本在 Ubuntu/Debian 上安装：

```bash
bash install.sh --install-docker
```

自定义 Codex 目录或端口：

```bash
bash install.sh --codex-home /home/ubuntu/.codex --port 8787
```

若已配置 HTTPS、认证和云防火墙，才允许公网监听：

```bash
bash install.sh --public --port 8787
```

若通过 HTTPS 反向代理访问，请将 `.env` 中的 `PANEL_COOKIE_SECURE` 改为 `true`，然后执行：

```bash
bash codex-panel restart
```

通过 SSH 隧道访问：

```bash
ssh -N -L 8787:127.0.0.1:8787 用户名@服务器IP
```

然后打开 `http://127.0.0.1:8787`。

## 日常管理

```bash
bash codex-panel status
bash codex-panel logs
bash codex-panel restart
bash codex-panel update
```

完整说明请查看 [`DEPLOYMENT.md`](DEPLOYMENT.md)。公网访问时，请放在带 HTTPS 和登录认证的 Nginx/Caddy 反向代理后面。
