"""Provider-domain primitives shared by the remote console and relay runtime.

This module deliberately has no FastAPI, Docker or Codex-runtime dependency.
It is the compatibility boundary between legacy console profiles and the
two-mode remote-provider model.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal, Mapping

PROFILE_SCHEMA_VERSION = 2
ProviderMode = Literal["official", "pure_api"]
ProviderProtocol = Literal["responses", "chat_completions"]


def canonical_mode(value: object) -> ProviderMode:
    """Translate legacy auth-mode values into the only supported modes."""
    if value in ("official", "chatgpt"):
        return "official"
    return "pure_api"


def canonical_protocol(value: object) -> ProviderProtocol:
    """Translate the legacy short protocol spelling into the public schema."""
    if value in ("chat", "chat_completions"):
        return "chat_completions"
    return "responses"


def legacy_auth_mode(mode: ProviderMode) -> str:
    """Compatibility output for Codex configuration generation during migration."""
    return "chatgpt" if mode == "official" else "apikey"


def legacy_wire_api(protocol: ProviderProtocol) -> str:
    """Compatibility output for current Codex config.toml writers."""
    return "chat" if protocol == "chat_completions" else "responses"


def normalize_profile(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return a version-2 profile without deleting legacy data.

    Existing installations store ``auth_mode`` and ``wire_api``.  Keeping those
    fields makes upgrades reversible while all new code can use ``mode`` and
    ``protocol``.  Official profiles have no upstream transport settings.
    """
    profile = deepcopy(dict(raw))
    # Pydantic includes optional version-2 fields as null for legacy browser
    # submissions. Null means "not supplied", so fall back to the v1 fields.
    mode = canonical_mode(profile.get("mode") or profile.get("auth_mode"))
    protocol = canonical_protocol(profile.get("protocol") or profile.get("wire_api"))
    profile["schema_version"] = PROFILE_SCHEMA_VERSION
    profile["mode"] = mode
    profile["protocol"] = protocol
    profile["auth_mode"] = legacy_auth_mode(mode)
    profile["wire_api"] = legacy_wire_api(protocol)
    profile.setdefault("models", [])
    # Model catalog tuning was removed from the console. Discard residual
    # client-side metadata during normalization so saving a profile completes
    # the migration for older installations.
    profile.pop("model_windows", None)
    profile.pop("model_auto_compact", None)
    profile.pop("model_metadata", None)
    profile.setdefault("provider_config_overrides", "")

    if mode == "official":
        # An official account does not use a custom upstream.  Retain old fields
        # only in a migration-safe backup, never as active transport settings.
        profile["base_url"] = ""
        profile["bearer_token"] = None
        profile["protocol"] = "responses"
        profile["wire_api"] = "responses"
    return profile


def is_profile_usable(profile: Mapping[str, Any]) -> tuple[bool, str]:
    """Validate the mode-specific minimum configuration before activation."""
    normalized = normalize_profile(profile)
    if not str(normalized.get("id", "")).strip():
        return False, "Provider id is required"
    if not str(normalized.get("name", "")).strip():
        return False, "Provider name is required"
    if normalized["mode"] == "official":
        return True, ""
    if not str(normalized.get("base_url", "")).strip():
        return False, "Pure API providers require an upstream base URL"
    if not normalized.get("bearer_token") and not normalized.get("no_auth", False):
        return False, "Pure API providers require an API key unless no_auth is enabled"
    return True, ""


def backfill_profile_model(profile: Mapping[str, Any], model: object) -> tuple[dict[str, Any], bool]:
    """Persist a live Codex default model back into its provider profile.

    The console owns provider-level defaults, not per-thread model state. This
    is used only while leaving an active provider, so returning to it restores
    the most recently observed ``config.toml`` model.
    """
    updated = normalize_profile(profile)
    observed = str(model or "").strip()
    if not observed or observed == str(updated.get("model") or "").strip():
        return updated, False
    updated["model"] = observed[:120]
    return updated, True


def resolve_host_codex_home(configured_path: object, user_home: object) -> str:
    """Resolve the absolute host Codex directory for host-side operations."""
    configured = str(configured_path or "").strip()
    if configured:
        if not configured.startswith("/") or "\x00" in configured:
            raise ValueError("宿主机 Codex 数据目录无效")
        return configured
    home = str(user_home or "").strip()
    if home.startswith("/"):
        return f"{home.rstrip('/')}/.codex"
    raise ValueError("未配置宿主机 Codex 数据目录")
