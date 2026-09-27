# 第七阶段运行验收清单

第七阶段用于发布前的真实环境验收。它不新增复杂运行时依赖，所有检查都围绕个人自托管的安装、升级、恢复和故障定位。

## 新安装

1. 复制 `.env.example` 为 `.env`，确认 `PANEL_BIND=127.0.0.1`。
2. 执行 `docker compose config --quiet`。
3. 执行 `docker compose up -d --build`，确认两个核心服务处于 running。
4. 浏览器完成登录，验证供应商测试和 Relay 健康检查。

## 升级与回滚

1. 升级前复制 `.env` 和 `CODEX_HOME_HOST` 指向的数据目录。
2. 执行 `codex-panel update`；失败时先保留容器和日志，不要删除数据目录。
3. 需要回滚时切换到上一个 Git 提交，再执行 `docker compose up -d --build --force-recreate`。

## 恢复演练

1. 在“备份恢复”页面确认备份包含配置、认证快照和会话范围。
2. 恢复前确认系统自动生成 `before_restore` 备份。
3. 恢复后检查当前供应商、模型、Relay 和历史会话列表。

## 故障诊断

在项目目录运行 `python scripts/diagnose.py`。该命令只读检查版本、`.env` 和 Compose 配置，不会重启、删除或修改容器与数据。
