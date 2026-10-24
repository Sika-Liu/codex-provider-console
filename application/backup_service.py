"""Backup and restore workflows for the single-node control panel."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from re import Pattern
from typing import Callable

from storage_ops import resolve_backup_directory


class BackupService:
    """Own backup files without depending on FastAPI or HTTP exceptions."""

    def __init__(
        self,
        *,
        codex_home: Path,
        backup_root: Path,
        metadata_name: str,
        backup_id_pattern: Pattern[str],
        config_path: Path,
        profile_path: Path,
        settings_path: Path,
        auth_path: Path,
        common_config_path: Path,
        session_index_path: Path,
        write_private: Callable[[Path, str], None],
    ) -> None:
        self.codex_home = codex_home
        self.backup_root = backup_root
        self.metadata_name = metadata_name
        self.backup_id_pattern = backup_id_pattern
        self.config_path = config_path
        self.profile_path = profile_path
        self.settings_path = settings_path
        self.auth_path = auth_path
        self.common_config_path = common_config_path
        self.session_index_path = session_index_path
        self.write_private = write_private

    def backup_state(self, *, include_sessions: bool = False, reason: str | None = None) -> tuple[str, Path]:
        created_at = datetime.now(timezone.utc)
        destination = self.backup_root / created_at.strftime("%Y%m%dT%H%M%S%fZ")
        destination.mkdir(parents=True, mode=0o700)
        self.write_private(
            destination / self.metadata_name,
            json.dumps(
                {"reason": reason or "manual", "created_at": created_at.isoformat(), "include_sessions": include_sessions},
                ensure_ascii=False,
            ) + "\n",
        )
        for source in (self.config_path, self.profile_path, self.settings_path, self.auth_path, self.common_config_path):
            if source.exists():
                target = destination / source.name
                shutil.copy2(source, target)
                os.chmod(target, 0o600)
        if include_sessions:
            (destination / "sessions").mkdir(mode=0o700)
            database = self.codex_home / "state_5.sqlite"
            if database.is_file():
                source_connection = sqlite3.connect(database, timeout=10)
                target_connection = sqlite3.connect(destination / database.name)
                try:
                    source_connection.backup(target_connection)
                finally:
                    target_connection.close()
                    source_connection.close()
                os.chmod(destination / database.name, 0o600)
            if self.session_index_path.is_file():
                target = destination / self.session_index_path.name
                shutil.copy2(self.session_index_path, target)
                os.chmod(target, 0o600)
        return destination.name, destination

    def prune_provider_switch_backups(self, retain: int) -> list[str]:
        if not self.backup_root.is_dir():
            return []
        managed: list[Path] = []
        for candidate in self.backup_root.iterdir():
            if not candidate.is_dir() or candidate.parent != self.backup_root:
                continue
            try:
                metadata = json.loads((candidate / self.metadata_name).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(metadata, dict) and metadata.get("reason") == "provider_switch":
                managed.append(candidate)
        removed: list[str] = []
        for candidate in sorted(managed, key=lambda path: path.name, reverse=True)[max(retain, 0):]:
            try:
                shutil.rmtree(candidate)
                removed.append(candidate.name)
            except OSError:
                continue
        return removed

    def prune_history(self, *, retain_complete: int = 1, retain_before_restore: int = 1) -> list[str]:
        if not self.backup_root.is_dir():
            return []
        candidates: list[tuple[Path, dict, set[str]]] = []
        for candidate in self.backup_root.iterdir():
            if not candidate.is_dir() or candidate.parent != self.backup_root or candidate.is_symlink():
                continue
            metadata: dict = {}
            try:
                raw = json.loads((candidate / self.metadata_name).read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    metadata = raw
            except (OSError, json.JSONDecodeError):
                pass
            try:
                files = {item.name for item in candidate.iterdir() if item.is_file() and item.name != self.metadata_name}
            except OSError:
                continue
            candidates.append((candidate, metadata, files))
        complete = [
            item for item in candidates
            if {"config.toml", self.profile_path.name, self.settings_path.name, self.auth_path.name}.issubset(item[2])
        ]
        before_restore = [item for item in candidates if item[1].get("reason") == "before_restore"]
        keep = {
            item[0] for item in sorted(complete, key=lambda item: item[0].name, reverse=True)[:max(retain_complete, 0)]
        }
        keep.update(
            item[0] for item in sorted(before_restore, key=lambda item: item[0].name, reverse=True)[:max(retain_before_restore, 0)]
        )
        removed: list[str] = []
        for candidate, _, _ in candidates:
            if candidate in keep:
                continue
            try:
                shutil.rmtree(candidate)
                removed.append(candidate.name)
            except OSError:
                continue
        return removed

    def restore_session_state(self, backup_dir: Path) -> None:
        database_backup = backup_dir / "state_5.sqlite"
        if database_backup.is_file():
            shutil.copy2(database_backup, self.codex_home / "state_5.sqlite")
            os.chmod(self.codex_home / "state_5.sqlite", 0o600)
        index_backup = backup_dir / self.session_index_path.name
        if index_backup.is_file():
            shutil.copy2(index_backup, self.session_index_path)
            os.chmod(self.session_index_path, 0o600)
        for directory_name in ("sessions", "archived_sessions"):
            source_root = backup_dir / directory_name
            if not source_root.is_dir():
                continue
            for source in source_root.rglob("*.jsonl"):
                target = self.codex_home / source.relative_to(backup_dir)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                os.chmod(target, 0o600)

    def backup_directory(self, backup_id: str) -> Path:
        return resolve_backup_directory(self.backup_root, backup_id, self.backup_id_pattern)

    def restore_state(self, backup_dir: Path) -> list[str]:
        restored: list[str] = []
        for source_name, target in (
            (self.config_path.name, self.config_path),
            (self.profile_path.name, self.profile_path),
            (self.settings_path.name, self.settings_path),
            (self.auth_path.name, self.auth_path),
            (self.common_config_path.name, self.common_config_path),
        ):
            source = backup_dir / source_name
            if source.is_file():
                self.write_private(target, source.read_text(encoding="utf-8"))
                restored.append(source_name)
            elif target.exists():
                target.unlink()
                restored.append(f"removed:{source_name}")
        if (backup_dir / "state_5.sqlite").is_file() or (backup_dir / self.session_index_path.name).is_file():
            self.restore_session_state(backup_dir)
            restored.append("sessions")
        return restored

    def list_backups(self) -> list[dict]:
        if not self.backup_root.exists():
            return []
        backups: list[dict] = []
        for path in sorted(self.backup_root.iterdir(), reverse=True):
            if not path.is_dir() or not self.backup_id_pattern.fullmatch(path.name):
                continue
            metadata: dict = {}
            try:
                raw_metadata = json.loads((path / self.metadata_name).read_text(encoding="utf-8"))
                if isinstance(raw_metadata, dict):
                    metadata = raw_metadata
            except (OSError, json.JSONDecodeError):
                pass
            files = [item.name for item in path.iterdir() if item.is_file() and item.name != self.metadata_name]
            backups.append({
                "id": path.name,
                "created_at": metadata.get("created_at", ""),
                "reason": metadata.get("reason", "unknown"),
                "include_sessions": bool(metadata.get("include_sessions")),
                "files": sorted(files),
                "has_config": "config.toml" in files,
            })
        return backups
