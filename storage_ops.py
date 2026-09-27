"""Safe filesystem primitives shared by the panel's storage workflows."""

import os
from pathlib import Path
from re import Pattern


def write_private(path: Path, text: str) -> None:
    """Atomically write a UTF-8 file with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".new")
    temp.write_text(text, encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def resolve_backup_directory(root: Path, backup_id: str, identifier: Pattern[str]) -> Path:
    """Resolve a direct backup child and reject traversal or symlinks."""
    if not identifier.fullmatch(backup_id):
        raise ValueError("invalid backup identifier")
    candidate = root / backup_id
    try:
        if candidate.parent.resolve() != root.resolve() or candidate.is_symlink() or not candidate.is_dir():
            raise FileNotFoundError(backup_id)
    except OSError as exc:
        raise FileNotFoundError(backup_id) from exc
    return candidate
