from typing import Literal
from pydantic import BaseModel, Field

class ModelEntry(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class Provider(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,48}$")
    name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(default="", pattern=r"^(|https?://.+)")
    wire_api: str | None = Field(default=None, pattern=r"^(responses|chat)$")
    model: str = Field(default="", max_length=120)
    auth_mode: Literal["apikey", "chatgpt"] | None = None
    # The version-2 schema is explicit about the two supported modes. Legacy
    # fields stay accepted while existing browser clients and stored profiles
    # migrate incrementally.
    mode: Literal["official", "pure_api"] | None = None
    protocol: Literal["responses", "chat_completions"] | None = None
    requires_openai_auth: bool = False
    bearer_token: str | None = Field(default=None, max_length=4096)
    no_auth: bool = False
    models: list[ModelEntry] = Field(default_factory=list)
    provider_config_overrides: str = Field(default="", max_length=50000)
    config_contents: str = Field(default="", max_length=50000)
    auth_contents: str = Field(default="", max_length=50000)
    goals_enabled: bool = False
    goals_configured: bool = False


class UpstreamModelFetch(BaseModel):
    base_url: str = Field(pattern=r"^https?://.+")
    bearer_token: str = Field(min_length=1, max_length=4096)


class ProviderDiagnosticRequest(BaseModel):
    id: str = ""
    name: str = ""
    base_url: str = ""
    wire_api: str | None = None
    model: str = ""
    auth_mode: Literal["apikey", "chatgpt"] | None = None
    mode: Literal["official", "pure_api"] | None = None
    protocol: Literal["responses", "chat_completions"] | None = None
    bearer_token: str | None = None
    no_auth: bool = False
    config_contents: str = ""
    auth_contents: str = ""


class ModelDiagnosticRequest(ProviderDiagnosticRequest):
    """An unsaved pure-API profile plus the catalog models to probe."""

    models: list[ModelEntry] = Field(default_factory=list)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


