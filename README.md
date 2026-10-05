# Codex Provider Console

通用的 Codex 供应商控制台，适合部署在安装了 Docker 的 Linux 云服务器上。

它可以管理：

- 官方登录供应商
- 自定义 API 供应商
- Codex 配置、认证和会话
- 供应商切换、备份恢复和健康检查

## 快速部署

### 1. 准备服务器

使用 Ubuntu、Debian、CentOS、RHEL、Rocky 或 AlmaLinux 等 Linux 系统，并使用非 root 用户登录。

如果服务器还没有 Docker，可以让安装器自动安装：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Sika-Liu/codex-provider-console/main/bootstrap.sh) --install-docker
```

### 2. 一键安装

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Sika-Liu/codex-provider-console/main/bootstrap.sh)
```

没有 `curl` 时可以使用：

```bash
bash <(wget -qO- https://raw.githubusercontent.com/Sika-Liu/codex-provider-console/main/bootstrap.sh)
```

安装过程中会询问面板端口，默认是 `8787`。首次安装还会生成管理员用户名、随机密码和会话密钥，并在终端中显示一次，请及时保存。

安装完成后，终端会显示：

- 公网访问地址
- 内网访问地址
- SSH 隧道命令
- 配置文件位置
- 云安全组提醒

## 首次使用

1. 使用安装输出的地址打开面板。
2. 输入安装时显示的管理员账号和密码。
3. 打开左侧“健康检查”，确认 Codex 目录、权限、CLI 和供应商连接正常。健康检查会比较官方最新 CLI 版本；如果版本落后，会显示“更新 Codex CLI”按钮。
4. 在“供应商配置”中添加或选择供应商。
5. 如需官方登录，进入对应供应商后点击“开始官方登录”。

面板左下角的用户名菜单可用于修改密码和退出登录。

## 公网访问安全

安装器默认监听：

```text
127.0.0.1:8787
```

这便于通过服务器公网 IP 访问，但请注意：

- 默认仅监听本机；通过 SSH 隧道访问最安全；
- 如需公网访问，显式设置 PANEL_BIND=0.0.0.0，并在云安全组中限制端口来源；
- 长期公网使用建议配置 HTTPS 反向代理；
- 使用 HTTPS 反向代理时，将 `.env` 中的 `PANEL_COOKIE_SECURE` 改为 `true`，然后执行 `codex-panel restart`；
- 不要将管理员密码、`.env`、`auth.json` 或备份文件分享给他人。

如只想本机访问，可将 `PANEL_BIND` 设置为 `127.0.0.1`，再通过 SSH 隧道访问：

```bash
ssh -N -L 8787:127.0.0.1:8787 用户名@服务器IP
```

然后打开 `http://127.0.0.1:8787`。

## 常用管理命令

```bash
codex-panel status
codex-panel logs
codex-panel restart
codex-panel update
codex-panel help
codex-panel uninstall
```

更新前建议备份 `.env` 和 Codex 数据目录。卸载时可以选择保留或删除 Codex 数据；如果选择删除，正在运行的 Codex App Server 可能会被停止，当前任务也可能中断。

卸载完成后可检查：

```bash
test ! -e ~/codex-provider-console && echo "控制台目录已删除"
test ! -e ~/.codex && echo "Codex 数据目录已删除"
test ! -e ~/.local/bin/codex-panel && echo "管理命令已删除"
```

## 数据位置

默认情况下，Codex 数据位于：

```text
~/.codex
```

安装项目位于：

```text
~/codex-provider-console
```

如需迁移到新服务器，请安全复制 Codex 数据目录，并在新部署中使用对应的 `CODEX_HOME_HOST`。

## 详细文档

- [完整部署与访问说明](DEPLOYMENT.md)
- [运行验收与故障诊断](docs/OPERATIONS.md)
- [升级、回滚与失败处理](docs/UPGRADING.md)
- [系统架构边界](docs/architecture.md)

本地开发或修改后可运行测试：

```bash
python -m unittest discover -s tests -v
```
