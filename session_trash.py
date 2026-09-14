"""Filesystem primitives for the control panel session recycle bin."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


MANIFEST_NAME = "manifest.json"


def _utc_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    return current if current.tzinfo else current.replace(tzinfo=timezone.utc)


def _write_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _safe_entry(root: Path, entry_id: str) -> Path:
    if not entry_id or entry_id in {".", ".."} or "/" in entry_id or "\\" in entry_id:
        raise ValueError("无效的回收站条目标识")
    root_resolved = root.resolve()
    entry = (root / entry_id).resolve()
    if entry.parent != root_resolved:
        raise ValueError("无效的回收站条目标识")
    return entry


def create_entry(
    trash_root: Path,
    codex_home: Path,
    thread_id: str,
    session_paths: list[Path],
    index_record: dict | None,
    *,
    retention_days: int = 7,
    now: datetime | None = None,
) -> dict:
    """Copy one remote session and its index record into a private recycle entry."""
    created = _utc_now(now)
    trash_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    entry_id = f"{created.strftime('%Y%m%dT%H%M%SZ')}-{thread_id}"
    entry = trash_root / entry_id
    if entry.exists():
        entry_id += f"-{uuid.uuid4().hex[:8]}"
        entry = trash_root / entry_id
    entry.mkdir(mode=0o700)

    copied: list[str] = []
    try:
        home = codex_home.resolve()
        for source in session_paths:
            source_resolved = source.resolve()
            try:
                relative = source_resolved.relative_to(home)
            except ValueError as exc:
                raise ValueError("会话文件不在 Codex 数据目录中") from exc
            if not source_resolved.is_file():
                raise FileNotFoundError(str(source))
            target = entry / "files" / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(source_resolved, target)
            os.chmod(target, 0o600)
            copied.append(relative.as_posix())
        manifest = {
            "schema_version": 1,
            "id": entry_id,
            "thread_id": thread_id,
            "deleted_at": created.isoformat(),
            "expires_at": (created + timedelta(days=max(1, retention_days))).isoformat(),
            "session_files": copied,
            "index_record": index_record if isinstance(index_record, dict) else None,
            "status": "ready",
        }
        _write_private_json(entry / MANIFEST_NAME, manifest)
        return manifest
    except Exception:
        shutil.rmtree(entry, ignore_errors=True)
        raise


def read_entry(trash_root: Path, entry_id: str) -> tuple[Path, dict]:
    entry = _safe_entry(trash_root, entry_id)
    manifest_path = entry / MANIFEST_NAME
    if not manifest_path.is_file():
        raise FileNotFoundError(entry_id)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("id") != entry_id:
        raise ValueError("回收站清单无效")
    return entry, manifest


def list_entries(trash_root: Path, *, now: datetime | None = None, purge_expired: bool = True) -> list[dict]:
    current = _utc_now(now)
    entries: list[dict] = []
    if not trash_root.is_dir():
        return entries
    for child in sorted(trash_root.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        try:
            _, manifest = read_entry(trash_root, child.name)
            expires = datetime.fromisoformat(str(manifest["expires_at"]))
            expires = expires if expires.tzinfo else expires.replace(tzinfo=timezone.utc)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
        if expires <= current:
            if purge_expired:
                shutil.rmtree(child)
            continue
        entries.append(manifest)
    return entries


def remove_entry(trash_root: Path, entry_id: str) -> dict:
    entry, manifest = read_entry(trash_root, entry_id)
    shutil.rmtree(entry)
    return manifest


def restore_entry(trash_root: Path, codex_home: Path, index_path: Path, entry_id: str) -> dict:
    """Restore an entry without overwriting an existing rollout or index record."""
    entry, manifest = read_entry(trash_root, entry_id)
    thread_id = str(manifest.get("thread_id") or "")
    relative_files = manifest.get("session_files") or []
    if not isinstance(relative_files, list):
        raise ValueError("回收站会话文件清单无效")

    existing_index: list[str] = []
    if index_path.is_file():
        existing_index = index_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        for line in existing_index:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(record.get("id") or "").lower() == thread_id.lower():
                raise FileExistsError("同 ID 的会话索引已经存在")

    home = codex_home.resolve()
    targets: list[tuple[Path, Path]] = []
    for relative_text in relative_files:
        relative = Path(str(relative_text))
        source = (entry / "files" / relative).resolve()
        target = (codex_home / relative).resolve()
        try:
            target.relative_to(home)
            source.relative_to((entry / "files").resolve())
        except ValueError as exc:
            raise ValueError("回收站会话路径无效") from exc
        if not source.is_file():
            raise FileNotFoundError(str(source))
        if target.exists():
            raise FileExistsError(f"会话文件已经存在：{relative_text}")
        targets.append((source, target))

    restored: list[Path] = []
    try:
        for source, target in targets:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(source, target)
            os.chmod(target, 0o600)
            restored.append(target)
        index_record = manifest.get("index_record")
        if isinstance(index_record, dict):
            index_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            contents = "".join(existing_index)
            if contents and not contents.endswith("\n"):
                contents += "\n"
            contents += json.dumps(index_record, ensure_ascii=False, separators=(",", ":")) + "\n"
            temporary = index_path.with_suffix(index_path.suffix + ".restore.tmp")
            temporary.write_text(contents, encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, index_path)
    except Exception:
        for target in reversed(restored):
            target.unlink(missing_ok=True)
        raise
    shutil.rmtree(entry)
    return manifest
