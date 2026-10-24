"""Pure activation-plan generation for a selected provider profile."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from typing import Any, Mapping


class ProviderActivationValidationError(ValueError):
    """The selected provider cannot produce a valid Codex activation plan."""


@dataclass(frozen=True)
class ProviderActivationPlan:
    config_text: str
    auth_text: str
    selected_model: str
    mode: str


def _toml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _validate_official_auth_snapshot(contents: str) -> None:
    try:
        parsed = json.loads(contents)
    except json.JSONDecodeError as exc:
        raise ProviderActivationValidationError("The captured authentication snapshot is invalid JSON") from exc
    if not isinstance(parsed, dict) or parsed.get("auth_mode") != "chatgpt":
        raise ProviderActivationValidationError("The current auth.json is not a ChatGPT login session")


def build_activation_plan(
    profile: Mapping[str, Any],
    *,
    base_root: str,
    merged_sections: str,
    session_provider_id: str,
    host_relay_base_url: str,
    model_override: str | None = None,
) -> ProviderActivationPlan:
    """Build the two files written during activation without host side effects."""
    mode = str(profile.get("mode") or "")
    auth_mode = str(profile.get("auth_mode") or "")
    if mode not in {"official", "pure_api"}:
        raise ProviderActivationValidationError("Provider mode is invalid")
    if mode == "pure_api" and not profile.get("bearer_token") and not profile.get("no_auth", False):
        raise ProviderActivationValidationError("An API key is required for a pure API provider")
    if mode == "official":
        snapshot = str(profile.get("auth_contents") or "")
        if not snapshot:
            raise ProviderActivationValidationError("Capture the current ChatGPT authentication before activating this profile")
        _validate_official_auth_snapshot(snapshot)
        auth_text = snapshot.rstrip() + "\n"
    else:
        api_auth = str(profile.get("auth_contents") or "").strip()
        if not api_auth:
            api_auth = json.dumps({"OPENAI_API_KEY": profile["bearer_token"]}, indent=2)
        auth_text = api_auth.rstrip() + "\n"

    selected_model = str(model_override or profile.get("model") or "").strip()
    generated_root = [f"model_provider = {_toml_quote(session_provider_id)}"]
    if selected_model:
        generated_root.append(f"model = {_toml_quote(selected_model)}")
    generated_provider = [
        f"[model_providers.{session_provider_id}]",
        f"name = {_toml_quote(session_provider_id)}",
        f"requires_openai_auth = {str(auth_mode == 'chatgpt').lower()}",
    ]
    if mode == "pure_api":
        generated_provider.extend([
            'wire_api = "responses"',
            'supports_websockets = false',
            f"base_url = {_toml_quote(host_relay_base_url)}",
            'experimental_bearer_token = "codex-provider-console-relay"',
        ])
    config_text = "\n\n".join(
        part for part in (base_root, "\n".join(generated_root), merged_sections, "\n".join(generated_provider)) if part
    )
    try:
        tomllib.loads(config_text)
    except tomllib.TOMLDecodeError as exc:
        raise ProviderActivationValidationError(f"Generated Codex config is invalid: {exc}") from exc
    return ProviderActivationPlan(
        config_text=config_text + "\n",
        auth_text=auth_text,
        selected_model=selected_model,
        mode=mode,
    )
