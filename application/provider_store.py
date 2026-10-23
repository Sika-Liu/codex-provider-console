"""Provider profile and panel-settings storage boundary."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable


class ProviderStore:
    """Read and sanitize provider state without depending on FastAPI routes."""

    def __init__(self, profile_path: Path, settings_path: Path, normalize: Callable[[dict], dict]):
        self.profile_path = profile_path
        self.settings_path = settings_path
        self.normalize = normalize

    def read_profiles(self) -> dict[str, dict]:
        if not self.profile_path.exists():
            return {}
        try:
            raw = json.loads(self.profile_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("profile store must be an object")
            return {
                profile_id: self.normalize(profile)
                for profile_id, profile in raw.items()
                if isinstance(profile_id, str) and isinstance(profile, dict)
            }
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("Provider profile store is invalid") from exc

    def panel_settings(self) -> dict[str, Any]:
        if not self.settings_path.exists():
            return {"provider_switching_enabled": True, "reverse_proxy": {}, "active_provider_id": ""}
        try:
            settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Control panel settings store is invalid") from exc
        if not isinstance(settings, dict):
            raise ValueError("Control panel settings store is invalid")
        return {
            "provider_switching_enabled": bool(settings.get("provider_switching_enabled", True)),
            "reverse_proxy": settings.get("reverse_proxy") if isinstance(settings.get("reverse_proxy"), dict) else {},
            "active_provider_id": str(settings.get("active_provider_id") or ""),
        }

    def active_config_provider(self, config_path: Path) -> str | None:
        if not config_path.exists():
            return None
        match = re.search(r"^\s*model_provider\s*=\s*['\"]([^'\"]+)['\"]", config_path.read_text(encoding="utf-8"), re.M)
        return match.group(1) if match else None

    def active_provider(self, config_path: Path) -> str | None:
        profiles = self.read_profiles()
        selected = self.panel_settings().get("active_provider_id", "")
        if selected in profiles:
            return selected
        legacy_provider = self.active_config_provider(config_path)
        return legacy_provider if legacy_provider in profiles else None

    def active_default_model(self, config_path: Path) -> str | None:
        if not config_path.exists():
            return None
        match = re.search(r"^\s*model\s*=\s*['\"]([^'\"]+)['\"]", config_path.read_text(encoding="utf-8"), re.M)
        return match.group(1).strip() if match and match.group(1).strip() else None

    def public_profile(self, profile: dict) -> dict:
        result = self.normalize(profile)
        result["has_bearer_token"] = bool(result.get("bearer_token"))
        result["has_auth_snapshot"] = bool(result.get("auth_contents"))
        result.pop("bearer_token", None)
        result.pop("auth_contents", None)
        return result
