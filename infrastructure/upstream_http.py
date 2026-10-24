"""Standard-library HTTP gateway for upstream provider diagnostics."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from model_diagnostic_domain import validate_model_diagnostic_response


class UpstreamModelFetchError(ValueError):
    pass


def upstream_endpoint(base_url: str, path: str) -> str:
    """Join an upstream base URL with an OpenAI v1 endpoint exactly once."""
    base = base_url.rstrip("/")
    return f"{base}{path}" if base.endswith("/v1") else f"{base}/v1{path}"


def upstream_endpoint_candidates(base_url: str, path: str) -> list[str]:
    """Return compatible endpoint forms for providers with non-standard Responses paths."""
    base = base_url.rstrip("/")
    canonical = upstream_endpoint(base, path)
    if path == "/responses" and not base.endswith("/v1"):
        return list(dict.fromkeys([f"{base}/responses", canonical]))
    return [canonical]


def summarize_upstream_error(body_text: str, limit: int = 240) -> str:
    """Keep useful provider error context without exposing a full upstream response."""
    text = (body_text or "").strip()
    if not text:
        return "上游未返回错误内容"
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message") or error.get("detail") or error.get("type")
                if message:
                    return str(message)[:limit]
            for key in ("message", "detail", "error"):
                if payload.get(key):
                    return str(payload[key])[:limit]
    except json.JSONDecodeError:
        pass
    return " ".join(text.split())[:limit]


def test_profile(profile: dict) -> dict:
    endpoint = upstream_endpoint(profile["base_url"], "/models")
    headers = {"Accept": "application/json", "User-Agent": "CodexProviderConsole/1.0"}
    if profile.get("bearer_token"):
        headers["Authorization"] = f'Bearer {profile["bearer_token"]}'
    request = urllib.request.Request(endpoint, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            return {"ok": 200 <= response.status < 300, "endpoint": endpoint, "status": response.status}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "endpoint": endpoint, "status": exc.code, "detail": "The provider rejected the model catalog request"}
    except urllib.error.URLError as exc:
        return {"ok": False, "endpoint": endpoint, "detail": f"Connection failed: {exc.reason}"}


MODEL_DIAGNOSTIC_TIMEOUT_SECONDS = 45
MODEL_DIAGNOSTIC_TIMEOUT_ATTEMPTS = 2
MODEL_DIAGNOSTIC_MAX_MODELS = 30
MODEL_DIAGNOSTIC_MAX_SECONDS = 180
MODEL_DIAGNOSTIC_JOB_TTL_SECONDS = 3600
MODEL_DIAGNOSTIC_MAX_JOBS = 30



def is_timeout_error(error: object) -> bool:
    """Recognize direct and urllib-wrapped network timeouts."""
    return isinstance(error, TimeoutError) or "timed out" in str(error).lower()


def test_model_request(
    profile: dict,
    test_model: str,
    *,
    timeout_seconds: int = MODEL_DIAGNOSTIC_TIMEOUT_SECONDS,
    max_attempts: int = MODEL_DIAGNOSTIC_TIMEOUT_ATTEMPTS,
) -> dict:
    base = profile["base_url"].rstrip("/")
    wire_api = profile.get("wire_api", "responses")
    suffix = "/chat/completions" if wire_api == "chat" else "/responses"
    endpoints = upstream_endpoint_candidates(base, suffix)
    payload = ({"model": test_model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 16}
               if wire_api == "chat" else {"model": test_model, "input": "hi", "max_output_tokens": 16})
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "CodexPlusPlus/RelayTest"}
    if profile.get("bearer_token"):
        headers["Authorization"] = f'Bearer {profile["bearer_token"]}'
    last_result = None
    for endpoint_index, endpoint in enumerate(endpoints):
        request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        for attempt in range(1, max_attempts + 1):
            try:
                with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                    body_text = response.read().decode("utf-8", errors="replace")
                    shape_ok, shape_detail = validate_model_diagnostic_response(body_text, wire_api)
                    return {"ok": 200 <= response.status < 300 and shape_ok, "endpoint": endpoint, "status": response.status, "detail": shape_detail, "error_kind": "" if shape_ok else "invalid_response", "attempts": attempt}
            except urllib.error.HTTPError as exc:
                body_text = exc.read().decode("utf-8", errors="replace")
                retry_after = (exc.headers.get("Retry-After") or "").strip()
                kind = ("rate_limited" if exc.code == 429 else "authentication_failed" if exc.code in {401, 403} else "model_or_endpoint_not_found" if exc.code == 404 else "upstream_gateway_error" if exc.code >= 500 else "upstream_http_error")
                detail = f"上游返回 HTTP {exc.code}：{summarize_upstream_error(body_text)}"
                if exc.code == 429:
                    detail = f"上游限流（HTTP 429），请稍后重试{f'；Retry-After: {retry_after} 秒' if retry_after.isdigit() else ''}。"
                last_result = {"ok": False, "endpoint": endpoint, "status": exc.code, "error_kind": kind, "retry_after": retry_after, "detail": detail, "attempts": attempt}
                if endpoint_index + 1 < len(endpoints) and exc.code in {404, 502, 503, 504}:
                    break
                return last_result
            except (TimeoutError, urllib.error.URLError) as exc:
                reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                if not is_timeout_error(reason):
                    return {"ok": False, "endpoint": endpoint, "detail": str(reason), "attempts": attempt, "error_kind": "upstream_connection_error"}
                if attempt < max_attempts:
                    continue
                last_result = {"ok": False, "endpoint": endpoint, "detail": f"上游在 {timeout_seconds} 秒内未返回响应（已尝试 {attempt} 次）", "error_kind": "upstream_timeout", "attempts": attempt}
                if endpoint_index + 1 < len(endpoints):
                    break
                return last_result
    if last_result is not None:
        return last_result
    raise AssertionError("diagnostic endpoint loop did not return")  # pragma: no cover


def test_model_request_via_relay(
    relay_base_url: str,
    test_model: str,
    *,
    timeout_seconds: int = MODEL_DIAGNOSTIC_TIMEOUT_SECONDS,
) -> dict:
    """Probe the active local Relay using the same Responses shape as Codex."""
    endpoint = f"{relay_base_url.rstrip('/')}/responses"
    payload = {"model": test_model, "input": "hi", "max_output_tokens": 16}
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "CodexProviderConsole/RelayDiagnostic"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body_text = response.read().decode("utf-8", errors="replace")
            shape_ok, shape_detail = validate_model_diagnostic_response(body_text, "responses")
            return {
                "ok": 200 <= response.status < 300 and shape_ok,
                "endpoint": endpoint,
                "status": response.status,
                "detail": shape_detail,
                "error_kind": "" if shape_ok else "invalid_relay_response",
            }
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "endpoint": endpoint, "status": exc.code, "detail": f"Relay 返回 HTTP {exc.code}", "error_kind": "relay_http_error"}
    except (TimeoutError, urllib.error.URLError) as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        return {"ok": False, "endpoint": endpoint, "detail": str(reason), "error_kind": "relay_connection_error"}



def fetch_upstream_models(base_url: str, bearer_token: str) -> dict:
    base = base_url.rstrip("/")
    candidates = [f"{base}/models"] if base.endswith("/v1") else [f"{base}/v1/models", f"{base}/models"]
    failures: list[str] = []
    for endpoint in dict.fromkeys(candidates):
        upstream_request = urllib.request.Request(
            endpoint,
            headers={"Accept": "application/json", "Authorization": f"Bearer {bearer_token}", "User-Agent": "CodexPlusPlus/RelayTest"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(upstream_request, timeout=12) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            failures.append(f"{endpoint}: HTTP {exc.code}")
            continue
        except TimeoutError:
            failures.append(f"{endpoint}: 上游在 12 秒内未返回模型列表")
            continue
        except (urllib.error.URLError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            failures.append(f"{endpoint}: {str(exc)}")
            continue
        entries = payload if isinstance(payload, list) else payload.get("data", payload.get("items", [])) if isinstance(payload, dict) else []
        if not isinstance(entries, list):
            failures.append(f"{endpoint}: unsupported response format")
            continue
        names: list[str] = []
        for item in entries:
            value = item if isinstance(item, str) else next((item.get(key) for key in ("id", "name", "model", "slug") if isinstance(item, dict) and isinstance(item.get(key), str) and item.get(key).strip()), None)
            if value and value not in names:
                names.append(value)
            if len(names) >= 300:
                break
        return {"endpoint": endpoint, "models": names}
    raise UpstreamModelFetchError("Unable to fetch upstream models. Check the Base URL, API Key, and model-list endpoint.")
