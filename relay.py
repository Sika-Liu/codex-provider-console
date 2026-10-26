"""Docker-internal Responses relay for pure API provider profiles."""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import uuid
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from provider_domain import normalize_profile
from relay_domain import chat_sse_to_responses_events, chat_to_response, relay_capabilities, resolve_active_profile, responses_to_chat_request, unsupported_request_features

CODEX_HOME = Path(os.environ.get("CODEX_HOME", "/codex"))
APP_VERSION = (Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip() if Path(__file__).with_name("VERSION").is_file() else "0.1.0")
CONFIG_PATH = CODEX_HOME / "config.toml"
PROFILE_PATH = CODEX_HOME / "control-panel-profiles.json"
SETTINGS_PATH = CODEX_HOME / "control-panel-settings.json"
app = FastAPI(title="Codex Provider Relay", version=APP_VERSION, docs_url=None, redoc_url=None)
REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


RELAY_MAX_BODY_BYTES = _positive_int("RELAY_MAX_BODY_BYTES", 1024 * 1024)
RELAY_UPSTREAM_TIMEOUT_SECONDS = _positive_int("RELAY_UPSTREAM_TIMEOUT_SECONDS", 120)
RELAY_CONCURRENCY_LIMIT = _positive_int("RELAY_CONCURRENCY_LIMIT", 4)
RELAY_SEMAPHORE = threading.BoundedSemaphore(RELAY_CONCURRENCY_LIMIT)
RELAY_STATUS_LOCK = threading.Lock()
RELAY_STATUS: dict[str, Any] = {
    "requests": 0, "failures": 0, "active": 0, "last_status": None,
    "last_error": "", "updated_at": 0.0,
}


def relay_status_snapshot() -> dict[str, Any]:
    with RELAY_STATUS_LOCK:
        return dict(RELAY_STATUS)


def record_upstream(status: int | None, detail: str = "") -> None:
    with RELAY_STATUS_LOCK:
        RELAY_STATUS["requests"] += 1
        RELAY_STATUS["last_status"] = status
        RELAY_STATUS["last_error"] = detail[:300]
        RELAY_STATUS["failures"] += int(status is None or status >= 400)
        RELAY_STATUS["updated_at"] = time.time()


def request_id_for(request: Request) -> str:
    supplied = request.headers.get("x-request-id", "")
    return supplied if REQUEST_ID.fullmatch(supplied) else uuid.uuid4().hex


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


def upstream_request(profile: dict[str, Any], path: str, body: dict[str, Any], request_id: str) -> tuple[int, dict[str, Any]]:
    endpoint = upstream_endpoint(str(profile["base_url"]), path)
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "CodexProviderConsole/Relay", "X-Request-Id": request_id}
    if profile.get("bearer_token"):
        headers["Authorization"] = f"Bearer {profile['bearer_token']}"
    request = urllib.request.Request(endpoint, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=RELAY_UPSTREAM_TIMEOUT_SECONDS) as response:
            status, payload = response.status, json.loads(response.read().decode("utf-8"))
            record_upstream(status)
            return status, payload
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        record_upstream(exc.code, detail)
        raise HTTPException(exc.code, detail or "Upstream rejected the request") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        record_upstream(None, str(exc))
        raise HTTPException(502, f"Upstream request failed: {exc}") from exc


def upstream_stream(
    profile: dict[str, Any],
    path: str,
    body: dict[str, Any],
    transform: Any = None,
    request_id: str = "",
    on_close: Any = None,
) -> StreamingResponse:
    """Forward an SSE stream, optionally translating it while keeping it live."""
    endpoint = upstream_endpoint(str(profile["base_url"]), path)
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream", "User-Agent": "CodexProviderConsole/Relay", "X-Request-Id": request_id}
    if profile.get("bearer_token"):
        headers["Authorization"] = f"Bearer {profile['bearer_token']}"
    request = urllib.request.Request(endpoint, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        response = urllib.request.urlopen(request, timeout=RELAY_UPSTREAM_TIMEOUT_SECONDS)
        record_upstream(response.status)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        record_upstream(exc.code, detail)
        raise HTTPException(exc.code, detail or "Upstream rejected the stream") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        record_upstream(None, str(exc))
        raise HTTPException(502, f"Upstream stream failed: {exc}") from exc

    def chunks():
        try:
            while chunk := response.read(8192):
                yield chunk
        finally:
            response.close()
            if on_close:
                on_close()

    stream = transform(chunks()) if transform else chunks()
    return StreamingResponse(stream, status_code=response.status, media_type="text/event-stream", headers={"X-Request-Id": request_id})


@app.get("/health")
def health() -> dict[str, Any]:
    profile = active_profile()
    protocol = str(profile["protocol"])
    return {"status": "ok", "provider_id": str(profile["id"]), "protocol": protocol, "capabilities": relay_capabilities(protocol), "upstream": relay_status_snapshot()}


@app.get("/capabilities")
def capabilities() -> dict[str, Any]:
    """Expose the relay's compatibility boundary for diagnostics and tooling."""
    profile = active_profile()
    return relay_capabilities(profile["protocol"])


@app.post("/v1/responses")
async def create_response(request: Request) -> JSONResponse:
    request_id = request_id_for(request)
    raw_body = await request.body()
    if len(raw_body) > RELAY_MAX_BODY_BYTES:
        raise HTTPException(413, "Responses request body exceeds the relay limit")
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(422, "Responses request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(422, "Responses request body must be a JSON object")
    profile = active_profile()
    unsupported = unsupported_request_features(body, profile["protocol"])
    if unsupported:
        raise HTTPException(422, {"type": "relay_unsupported_feature", "features": unsupported})
    if not RELAY_SEMAPHORE.acquire(blocking=False):
        raise HTTPException(429, "Relay is busy; retry shortly")
    with RELAY_STATUS_LOCK:
        RELAY_STATUS["active"] += 1
    release = True
    def done() -> None:
        with RELAY_STATUS_LOCK:
            RELAY_STATUS["active"] -= 1
        RELAY_SEMAPHORE.release()
    try:
        if body.get("stream"):
            if profile["protocol"] == "responses":
                stream = await asyncio.to_thread(upstream_stream, profile, "/responses", body, None, request_id, done)
            else:
                stream = await asyncio.to_thread(
                    upstream_stream, profile, "/chat/completions", responses_to_chat_request(body),
                    chat_sse_to_responses_events, request_id, done,
                )
            release = False
            return stream
        if profile["protocol"] == "responses":
            status, payload = await asyncio.to_thread(upstream_request, profile, "/responses", body, request_id)
        else:
            status, payload = await asyncio.to_thread(
                upstream_request, profile, "/chat/completions", responses_to_chat_request(body), request_id
            )
            payload = chat_to_response(payload)
        return JSONResponse(payload, status_code=status, headers={"X-Request-Id": request_id})
    finally:
        if release:
            done()
