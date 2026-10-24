"""Provider profile persistence workflows independent of FastAPI routes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from application.provider_store import ProviderStore
from provider_domain import normalize_profile, storage_profile


class ProviderService:
    """Save and delete provider profiles while preserving credential behavior."""

    def __init__(
        self,
        *,
        store: ProviderStore,
        profile_path: Path,
        write_private: Callable[[Path, str], None],
        create_backup: Callable[..., object],
    ) -> None:
        self.store = store
        self.profile_path = profile_path
        self.write_private = write_private
        self.create_backup = create_backup

    @staticmethod
    def _api_key_from_auth_contents(contents: str) -> str | None:
        if not contents.strip():
            return None
        try:
            parsed = json.loads(contents)
        except json.JSONDecodeError as exc:
            raise ValueError("auth.json must contain valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("auth.json must contain a JSON object")
        key = parsed.get("OPENAI_API_KEY")
        return key if isinstance(key, str) and key else None

    def _write_profiles(self, profiles: Mapping[str, Mapping[str, Any]]) -> None:
        payload = {key: storage_profile(value) for key, value in profiles.items()}
        self.write_private(self.profile_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    def save(self, incoming: Mapping[str, Any]) -> dict:
        profiles = self.store.read_profiles()
        data = normalize_profile(dict(incoming))
        available_models = {entry["name"] for entry in data["models"]}
        if data["mode"] == "pure_api" and data["model"] and data["model"] not in available_models:
            raise ValueError("配置模型必须从上游获取的模型列表中选择")
        provider_id = str(data["id"])
        existing = profiles.get(provider_id, {})
        if data["mode"] == "pure_api":
            entered_key = data["bearer_token"]
            key_from_auth = self._api_key_from_auth_contents(data["auth_contents"])
            if key_from_auth:
                data["bearer_token"] = key_from_auth
            elif not entered_key:
                data["bearer_token"] = existing.get("bearer_token")
                if existing.get("auth_contents"):
                    data["auth_contents"] = existing["auth_contents"]
            elif not data["auth_contents"] and existing.get("auth_contents"):
                data["auth_contents"] = existing["auth_contents"]
        elif not data["auth_contents"] and existing.get("auth_contents"):
            data["auth_contents"] = existing["auth_contents"]
        if not data["bearer_token"] and provider_id in profiles:
            data["bearer_token"] = profiles[provider_id].get("bearer_token")
        data["requires_openai_auth"] = data["mode"] == "official"
        self.create_backup(reason="provider_save")
        profiles[provider_id] = data
        self._write_profiles(profiles)
        return self.store.public_profile(data)

    def delete(self, provider_id: str, *, active_provider_id: str | None) -> None:
        if active_provider_id == provider_id:
            raise RuntimeError("Switch away from the active provider before deleting it")
        profiles = self.store.read_profiles()
        if provider_id not in profiles:
            raise LookupError("Provider profile not found")
        self.create_backup(reason="provider_delete")
        del profiles[provider_id]
        self._write_profiles(profiles)
