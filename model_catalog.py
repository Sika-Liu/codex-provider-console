"""Build Codex model_catalog_json payloads from a remote provider profile."""

from __future__ import annotations

import re
from typing import Any, Mapping


_TOKEN_LIMIT = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmg]?)$", re.IGNORECASE)


def parse_token_limit(value: Any) -> int | None:
    """Accept a token count or a human-friendly K/M/G suffix as an integer."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    match = _TOKEN_LIMIT.fullmatch(str(value).strip())
    if not match:
        return None
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000, "g": 1_000_000_000}[match.group(2).lower()]
    parsed = int(float(match.group(1)) * multiplier)
    return parsed if parsed > 0 else None


def build_model_catalog(profile: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    entries = profile.get("models", [])
    windows = profile.get("model_windows", {}) if isinstance(profile.get("model_windows"), dict) else {}
    compact = profile.get("model_auto_compact", {}) if isinstance(profile.get("model_auto_compact"), dict) else {}
    metadata = profile.get("model_metadata", {}) if isinstance(profile.get("model_metadata"), dict) else {}
    seen: set[str] = set()
    models: list[dict[str, Any]] = []
    candidates = list(entries) if isinstance(entries, list) else []
    default_model = str(profile.get("model", "")).strip()
    if default_model:
        candidates.append({"name": default_model})
    for entry in candidates:
        name = str(entry.get("name", "")).strip() if isinstance(entry, dict) else str(entry).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        context_window = parse_token_limit((entry.get("context_window") if isinstance(entry, dict) else "") or windows.get(name, ""))
        # A catalog entry without a context limit is not useful and has caused
        # compatibility problems in older Codex clients. Catalog generation is
        # therefore explicit opt-in per model.
        if not context_window:
            continue
        item: dict[str, Any] = {"slug": name, "display_name": name, "context_window": context_window, "max_context_window": context_window, "supported_in_api": True, "visibility": "list"}
        compact_limit = parse_token_limit((entry.get("auto_compact_limit") if isinstance(entry, dict) else "") or compact.get(name, ""))
        item["auto_compact_token_limit"] = compact_limit
        if isinstance(metadata.get(name), dict):
            item.update(metadata[name])
            item["slug"] = name
        models.append(item)
    return {"models": models}
