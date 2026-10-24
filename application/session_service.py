"""Session operations independent from FastAPI routes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


class SessionError(ValueError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status


@dataclass
class SessionService:
    valid_id: Callable[[str], bool]
    list_active: Callable[[], list[dict]]
    list_archived: Callable[[], list[dict]]
    start_app_server: Callable[[], str]
    archive_remote: Callable[[str, bool], str]
    delete_remote: Callable[[str], str]
    confirm_archive: Callable[[str, bool], None]
    migrate: Callable[[str | None, bool, object | None], dict[str, int]]
    backup: Callable[..., tuple[str, object]]
    profile_exists: Callable[[str], bool]
    audit: Callable[..., None]

    def _id(self, thread_id: str) -> str:
        value = thread_id.lower()
        if not self.valid_id(value):
            raise SessionError(422, "无效的会话标识")
        return value

    def active(self) -> dict:
        sessions = self.list_active()
        return {"sessions": sessions, "count": len(sessions)}

    def archived(self) -> dict:
        sessions = self.list_archived()
        return {"sessions": sessions, "count": len(sessions)}

    def archive(self, thread_id: str, archived: bool) -> dict:
        value = self._id(thread_id)
        source = self.list_active() if archived else self.list_archived()
        if not any(item["id"] == value for item in source):
            raise SessionError(404, "活动会话不存在或已经归档" if archived else "归档会话不存在或已经取消归档")
        try:
            self.start_app_server()
            self.archive_remote(value, archived)
        except RuntimeError as exc:
            raise SessionError(502, str(exc)) from exc
        self.confirm_archive(value, archived)
        self.audit("server_session_archived" if archived else "server_session_unarchived", thread_id=value, command="host codex archive --remote unix://")
        if archived:
            return {"archived": value, "sync": "pending", "detail": "归档命令已提交，会话将从列表中移除。"}
        return {"unarchived": value, "desktop_sync": "client_refresh_required", "sync": "pending", "detail": "取消归档命令已提交，会话将重新出现在活动列表。"}

    def delete(self, thread_id: str) -> dict:
        value = self._id(thread_id)
        try:
            self.start_app_server()
            self.delete_remote(value)
        except RuntimeError as exc:
            raise SessionError(502, str(exc)) from exc
        self.audit("server_session_deleted", thread_id=value, command="host codex delete --remote unix:// --force", recoverable=False)
        return {"deleted": value, "recoverable": False, "detail": "远程会话已通过宿主机 Codex App Server 删除；桌面端对应的远程缓存将同步移除。"}

    def migrate_provider(self, target_provider: str, source_provider: str | None, apply: bool, stable_provider: str) -> dict:
        if not self.profile_exists(target_provider):
            raise SessionError(404, "Target provider profile not found")
        backup_id, backup_dir = (self.backup(include_sessions=True, reason="session_migration") if apply else (None, None))
        result = self.migrate(source_provider, apply, backup_dir)
        self.audit("session_provider_migrated" if apply else "session_provider_migration_previewed", provider_id=target_provider, detail=f"session_provider={stable_provider}; source={source_provider or '*'}; changes={sum(result.values())}")
        changed = result["threads"] + result["rollout_records"]
        return {"target_provider": target_provider, "session_provider": stable_provider, "source_provider": source_provider, "applied": apply, "backup_id": backup_id, "matching_sessions": changed, "changed_sessions": changed, "details": result}
