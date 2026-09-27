# 升级与失败处理

个人部署建议使用项目自带的 `codex-panel update`：它先执行 `git pull --ff-only`，再重新构建并强制重建容器。

- Git 快进失败时，命令会立即停止，不会覆盖当前工作树，也不会继续重建容器。
- 构建或启动失败时，保留旧容器和日志现场；先运行 `codex-panel logs`、`docker compose ps` 和 `docker compose config` 定位问题。
- 升级前建议复制 `codex-data` 和 `.env`；面板内的“备份恢复”可用于恢复配置、认证快照和会话状态。
- 回滚时切换到已知可用的 Git 提交，再执行 `docker compose up -d --build --force-recreate`。
- 不要删除 `codex-data`、`.env` 或 `reverse-proxy` 目录来“修复”升级问题；这些目录包含个人配置和凭据。
