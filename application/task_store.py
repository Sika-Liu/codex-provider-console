"""Durable, single-instance background job state backed by SQLite."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable


DEFAULT_FINISHED_RETENTION_SECONDS = 7 * 24 * 60 * 60
DEFAULT_MAX_FINISHED_JOBS = 200


class PersistentJob(dict):
    """A job mapping that durably records whole-state changes."""

    def __init__(self, payload: dict, save: Callable[[dict], None]):
        dict.__init__(self, payload)
        self._save = save

    def persist(self) -> None:
        dict.__setitem__(self, "updated_at", time.time())
        self._save(dict(self))

    def update(self, *args, **kwargs) -> None:
        dict.update(self, *args, **kwargs)
        self.persist()

    def __setitem__(self, key, value) -> None:
        dict.__setitem__(self, key, value)
        self.persist()


class TaskStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS background_jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    def save(self, kind: str, job: dict) -> None:
        payload = dict(job)
        payload["updated_at"] = time.time()
        with self.lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO background_jobs(id, kind, status, payload, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind=excluded.kind,
                    status=excluded.status,
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                (payload["id"], kind, str(payload.get("status", "")),
                 json.dumps(payload, ensure_ascii=False, separators=(",", ":")), payload["updated_at"]),
            )

    def recover(self) -> list[tuple[str, dict]]:
        """Fail closed: a process restart cannot mean an old thread is still running."""
        now = time.time()
        recovered: list[tuple[str, dict]] = []
        with self.lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT kind, payload FROM background_jobs ORDER BY updated_at ASC"
            ).fetchall()
            for kind, raw in rows:
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if payload.get("status") == "running":
                    payload.update(
                        status="interrupted",
                        stage="服务重启，任务已中断",
                        detail="后台线程已因服务重启停止；请重新执行该任务。",
                        updated_at=now,
                    )
                    connection.execute(
                        "UPDATE background_jobs SET status=?, payload=?, updated_at=? WHERE id=?",
                        ("interrupted", json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                         now, payload.get("id")),
                    )
                recovered.append((kind, payload))
        return recovered

    def prune_finished(
        self,
        now: float | None = None,
        retention_seconds: float = DEFAULT_FINISHED_RETENTION_SECONDS,
        max_finished: int = DEFAULT_MAX_FINISHED_JOBS,
    ) -> int:
        """Bound historical records without ever removing a live task."""
        now = time.time() if now is None else now
        cutoff = now - retention_seconds
        with self.lock, self._connect() as connection:
            removed = connection.execute(
                "DELETE FROM background_jobs WHERE status != 'running' AND updated_at < ?",
                (cutoff,),
            ).rowcount
            overflow = connection.execute(
                """
                SELECT id FROM background_jobs
                WHERE status != 'running'
                ORDER BY updated_at DESC
                LIMIT -1 OFFSET ?
                """,
                (max_finished,),
            ).fetchall()
            if overflow:
                connection.executemany(
                    "DELETE FROM background_jobs WHERE id=?",
                    overflow,
                )
                removed += len(overflow)
        return removed

    def new_job(self, kind: str, payload: dict) -> PersistentJob:
        self.prune_finished()
        job = PersistentJob(payload, lambda value: self.save(kind, value))
        job.persist()
        return job
