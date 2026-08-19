# Codex Provider Console

通用的 Codex 供应商控制台，可部署到任意安装了 Docker 的 Linux 云服务器。

## 快速部署

```bash
git clone https://github.com/Sika-Liu/codex-provider-console.git
cd codex-provider-console
cp .env.example .env
```

编辑 `.env`：

```env
CODEX_HOME_HOST=/home/你的用户名/.codex
PANEL_BIND=127.0.0.1
PANEL_PORT=8787
PUID=1000
PGID=1000
```

启动：

```bash
docker compose up -d --build
docker compose ps
```

通过 SSH 隧道访问：

```bash
ssh -N -L 8787:127.0.0.1:8787 用户名@服务器IP
```

然后打开 `http://127.0.0.1:8787`。

完整说明请查看 [`DEPLOYMENT.md`](DEPLOYMENT.md)。公网访问时，请放在带 HTTPS 和登录认证的 Nginx/Caddy 反向代理后面。
