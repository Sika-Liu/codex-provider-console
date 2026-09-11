"""Docker-internal Responses relay for pure API provider profiles."""

from __future__ import annotations

import json
import os
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from provider_domain import normalize_profile
from relay_domain import chat_sse_to_responses_events, chat_to_response, resolve_active_profile, responses_to_chat_request

CODEX_HOME = Path(os.environ.get("CODEX_HOME", "/codex"))
CONFIG_PATH = CODEX_HOME / "config.toml"
PROFILE_PATH = CODEX_HOME / "control-panel-profiles.json"
SETTINGS_PATH = CODEX_HOME / "control-panel-settings.json"
app = FastAPI(title="Codex Provider Relay", docs_url=None, redoc_url=None)


def upstream_endpoint(base_url: str, path: str) -> str:
    """Join a provider base URL with an OpenAI API path exactly once."""
    base = base_url.rstrip("/")
    return f"{base}{path}" if base.endswith("/v1") else f"{base}/v1{path}"


def active_profile() -> dict[str, Any]:
    try:
        config = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        profiles = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(503, "The active provider configuration is unavailable") from exc
    try:
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")) if SETTINGS_PATH.exists() else {}
    except (OSError, json.JSONDecodeError):
        settings = {}
    profile = resolve_active_profile(profiles, config, settings)
    if not isinstance(profile, dict):
        raise HTTPException(503, "The active provider profile is unavailable")
    profile = normalize_profile(profile)
    if profile["mode"] != "pure_api":
        raise HTTPException(409, "The active provider uses official login and does not use the relay")
    return profile


def upstream_request(profile: dict[str, Any], path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    endpoint = upstream_endpoint(str(profile["base_url"]), path)
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "CodexProviderConsole/Relay"}
    if profile.get("bearer_token"):
        headers["Authorization"] = f"Bearer {profile['bearer_token']}"
    request = urllib.request.Request(endpoint, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise HTTPException(exc.code, detail or "Upstream rejected the request") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HTTPException(502, f"Upstream request failed: {exc}") from exc


def upstream_stream(
    profile: dict[str, Any],
    path: str,
    body: dict[str, Any],
    transform: Any = None,
) -> StreamingResponse:
    """Forward an SSE stream, optionally translating it while keeping it live."""
    endpoint = upstream_endpoint(str(profile["base_url"]), path)
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream", "User-Agent": "CodexProviderConsole/Relay"}
    if profile.get("bearer_token"):
        headers["Authorization"] = f"Bearer {profile['bearer_token']}"
    request = urllib.request.Request(endpoint, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        response = urllib.request.urlopen(request, timeout=120)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise HTTPException(exc.code, detail or "Upstream rejected the stream") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise HTTPException(502, f"Upstream stream failed: {exc}") from exc

    def chunks():
        try:
            while chunk := response.read(8192):
                yield chunk
        finally:
            response.close()

    stream = transform(chunks()) if transform else chunks()
    return StreamingResponse(stream, status_code=response.status, media_type="text/event-stream")


@app.get("/health")
def health() -> dict[str, str]:
    profile = active_profile()
    return {"status": "ok", "provider_id": str(profile["id"]), "protocol": str(profile["protocol"])}


@app.post("/v1/responses")
async def create_response(request: Request) -> JSONResponse:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(422, "Responses request body must be a JSON object")
    profile = active_profile()
    if body.get("stream"):
        if profile["protocol"] == "responses":
            return upstream_stream(profile, "/responses", body)
        return upstream_stream(
            profile,
            "/chat/completions",
            responses_to_chat_request(body),
            transform=chat_sse_to_responses_events,
        )
    if profile["protocol"] == "responses":
        status, payload = upstream_request(profile, "/responses", body)
    else:
        status, payload = upstream_request(profile, "/chat/completions", responses_to_chat_request(body))
        payload = chat_to_response(payload)
    return JSONResponse(payload, status_code=status)
