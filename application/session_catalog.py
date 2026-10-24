"""Read-only Codex session catalog operations."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from re import Pattern


@dataclass(frozen=True)
class SessionCatalogPaths:
    codex_home: Path
    sessions_path: Path
    archived_sessions_path: Path
    session_index_path: Path
    thread_id: Pattern[str]
    thread_id_in_text: Pattern[str]


class SessionCatalog:
    """Read session files and App Server registration without HTTP concerns."""

    def __init__(self, paths: SessionCatalogPaths) -> None:
        self._paths = paths

    def session_id_from_path(self, path: Path) -> str | None:
        match = self._paths.thread_id_in_text.search(path.name)
        return match.group(1).lower() if match else None

    def index_titles(self) -> dict[str, str]:
        titles: dict[str, str] = {}
        if not self._paths.session_index_path.exists():
            return titles
        try:
            with self._paths.session_index_path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    thread_id = str(record.get("id") or "").lower()
                    title = record.get("thread_name")
                    if self._paths.thread_id.fullmatch(thread_id) and isinstance(title, str) and title.strip():
                        titles[thread_id] = title.strip().replace("\n", " ")[:180]
        except OSError:
            pass
        return titles

    def index_records(self) -> dict[str, dict]:
        records: dict[str, dict] = {}
        if not self._paths.session_index_path.exists():
            return records
        try:
            with self._paths.session_index_path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    thread_id = str(record.get("id") or "").lower()
                    if self._paths.thread_id.fullmatch(thread_id):
                        records[thread_id] = record
        except OSError:
            pass
        return records

    def app_server_thread_states(self) -> dict[str, bool] | None:
        candidates: list[tuple[int, Path]] = []
        for path in self._paths.codex_home.glob("state_*.sqlite"):
            match = re.fullmatch(r"state_(\d+)\.sqlite", path.name)
            if match:
                candidates.append((int(match.group(1)), path))
        for _, path in sorted(candidates, reverse=True):
            try:
                uri = f"file:{path.resolve().as_posix()}?mode=ro"
                with sqlite3.connect(uri, uri=True) as connection:
                    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(threads)")}
                    if not {"id", "archived"}.issubset(columns):
                        continue
                    return {
                        str(thread_id).lower(): bool(archived)
                        for thread_id, archived in connection.execute("SELECT id, archived FROM threads")
                        if self._paths.thread_id.fullmatch(str(thread_id).lower())
                    }
            except (OSError, sqlite3.Error):
                continue
        return None

    def read_summary(self, path: Path, indexed_title: str = "") -> dict:
        title = ""
        cwd = ""
        provider_id = ""
        try:
            with path.open("r", encoding="utf-8") as handle:
                for _ in range(24):
                    line = handle.readline()
                    if not line:
                        break
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = record.get("payload") if isinstance(record.get("payload"), dict) else record
                    if not cwd:
                        cwd = str(payload.get("cwd") or record.get("cwd") or "")[:500]
                    if not provider_id:
                        provider_id = str(payload.get("model_provider") or record.get("model_provider") or "")[:80]
                    if not title:
                        for key in ("title", "session_title", "summary"):
                            value = payload.get(key) or record.get(key)
                            if isinstance(value, str) and value.strip():
                                title = value.strip().replace("\n", " ")[:180]
                                break
        except (OSError, UnicodeDecodeError):
            pass
        stat = path.stat()
        return {
            "id": self.session_id_from_path(path),
            "title": indexed_title or title or path.stem,
            "cwd": cwd,
            "provider_id": provider_id,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "size": stat.st_size,
            "path": str(path.relative_to(self._paths.codex_home)),
        }

    def list_active(self) -> list[dict]:
        sessions: list[dict] = []
        thread_states = self.app_server_thread_states()
        indexes = self.index_records()
        titles = {thread_id: str(record.get("thread_name") or "").strip().replace("\n", " ")[:180] for thread_id, record in indexes.items()}
        rollout_ids: set[str] = set()
        archived_ids = {
            thread_id
            for path in self._paths.archived_sessions_path.rglob("*.jsonl") if self._paths.archived_sessions_path.is_dir()
            if (thread_id := self.session_id_from_path(path))
        }
        for path in self._paths.sessions_path.rglob("*.jsonl") if self._paths.sessions_path.is_dir() else ():
            thread_id = self.session_id_from_path(path)
            if thread_id and (thread_states is None or thread_states.get(thread_id) is False):
                rollout_ids.add(thread_id)
                session = self.read_summary(path, titles.get(thread_id, ""))
                session["kind"] = "rollout"
                sessions.append(session)
        for thread_id, record in indexes.items():
            if thread_states is not None and thread_states.get(thread_id) is not False:
                continue
            if thread_id in rollout_ids or thread_id in archived_ids:
                continue
            sessions.append({
                "id": thread_id,
                "title": titles.get(thread_id) or thread_id,
                "cwd": "",
                "provider_id": "",
                "modified_at": str(record.get("updated_at") or ""),
                "size": 0,
                "path": "session_index.jsonl",
                "kind": "orphaned_index",
            })
        return sorted(sessions, key=lambda item: item["modified_at"], reverse=True)

    def list_archived(self) -> list[dict]:
        titles = self.index_titles()
        thread_states = self.app_server_thread_states()
        sessions: list[dict] = []
        for path in self._paths.archived_sessions_path.rglob("*.jsonl") if self._paths.archived_sessions_path.is_dir() else ():
            thread_id = self.session_id_from_path(path)
            if not thread_id or (thread_states is not None and thread_states.get(thread_id) is not True):
                continue
            session = self.read_summary(path, titles.get(thread_id, ""))
            session["kind"] = "archived"
            sessions.append(session)
        return sorted(sessions, key=lambda item: item["modified_at"], reverse=True)
