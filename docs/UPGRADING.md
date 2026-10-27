# 升级与失败处理

个人部署建议先备份 `.env` 和 Codex 数据，再使用项目自带的 `codex-panel update <git-ref>` 指定明确的 Git 标签、分支或 commit；命令会拉取该引用、切换到对应版本，再重新构建并强制重建容器。

- 未指定 Git 引用、工作树存在本地修改或 Git 引用不存在时，命令会立即停止，不会继续重建容器。
- 构建或启动失败时，保留旧容器和日志现场；先运行 `codex-panel logs`、`docker compose ps` 和 `docker compose config` 定位问题。
- 升级前建议复制 `codex-data` 和 `.env`；面板内的“备份恢复”可用于恢复配置、认证快照和会话状态。
- 回滚时执行 `codex-panel update <已知可用的 Git commit>`；例如 `codex-panel update 1a84a02608a00b26f112c183143810410799cae7`。
- 不要删除 `codex-data`、`.env` 或 `reverse-proxy` 目录来“修复”升级问题；这些目录包含个人配置和凭据。
