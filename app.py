import json
import os
import re
import shutil
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

CODEX_HOME = Path(os.environ.get("CODEX_HOME", "/codex"))
CONFIG_PATH = CODEX_HOME / "config.toml"
PROFILE_PATH = CODEX_HOME / "control-panel-profiles.json"
SETTINGS_PATH = CODEX_HOME / "control-panel-settings.json"
AUTH_PATH = CODEX_HOME / "auth.json"
BACKUP_ROOT = CODEX_HOME / "backups" / "control-panel"
AUDIT_PATH = CODEX_HOME / "control-panel-audit.jsonl"
PROFILE_ID = re.compile(r"^[a-zA-Z0-9_-]{1,48}$")

app = FastAPI(title="Codex Provider Console", docs_url=None, redoc_url=None)


class ModelEntry(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class Provider(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,48}$")
    name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(default="", pattern=r"^(|https?://.+)")
    wire_api: str = Field(default="responses", pattern=r"^(responses|chat)$")
    model: str = Field(default="gpt-5.6-terra", min_length=1, max_length=120)
    auth_mode: Literal["apikey", "chatgpt"] = "apikey"
    requires_openai_auth: bool = False
    bearer_token: str | None = Field(default=None, max_length=4096)
    models: list[ModelEntry] = Field(default_factory=list)
    config_contents: str = Field(default="", max_length=50000)
    auth_contents: str = Field(default="", max_length=50000)
    goals_enabled: bool = False
    goals_configured: bool = False
    test_model: str = Field(default="", max_length=120)


class UpstreamModelFetch(BaseModel):
    base_url: str = Field(pattern=r"^https?://.+")
    bearer_token: str = Field(min_length=1, max_length=4096)


class ProviderDiagnosticRequest(BaseModel):
    id: str = ""
    name: str = ""
    base_url: str = ""
    wire_api: str = "responses"
    model: str = ""
    auth_mode: Literal["apikey", "chatgpt"] = "apikey"
    bearer_token: str | None = None
    config_contents: str = ""
    auth_contents: str = ""
    test_model: str = ""


def read_profiles() -> dict[str, dict]:
    if not PROFILE_PATH.exists():
        return {}
    try:
        return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(500, "Provider profile store is invalid") from exc


def panel_settings() -> dict:
    if not SETTINGS_PATH.exists():
        return {"provider_switching_enabled": True, "ssh": {}, "reverse_proxy": {}}
    try:
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(500, "Control panel settings store is invalid") from exc
    return {
        "provider_switching_enabled": bool(settings.get("provider_switching_enabled", True)),
        "ssh": settings.get("ssh") if isinstance(settings.get("ssh"), dict) else {},
        "reverse_proxy": settings.get("reverse_proxy") if isinstance(settings.get("reverse_proxy"), dict) else {},
    }


def write_private(path: Path, text: str) -> None:
    temp = path.with_suffix(path.suffix + ".new")
    temp.write_text(text, encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def backup_state(include_sessions: bool = False) -> tuple[str, Path]:
    destination = BACKUP_ROOT / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination.mkdir(parents=True, mode=0o700)
    for source in (CONFIG_PATH, PROFILE_PATH, AUTH_PATH):
        if source.exists():
            target = destination / source.name
            shutil.copy2(source, target)
            os.chmod(target, 0o600)
    if include_sessions:
        (destination / "sessions").mkdir(mode=0o700)
    return destination.name, destination


def audit(action: str, **details: str | bool | None) -> None:
    payload = {"timestamp": datetime.now(timezone.utc).isoformat(), "action": action, **details}
    with AUDIT_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    os.chmod(AUDIT_PATH, 0o600)


def toml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def remove_provider_sections(config: str) -> str:
    output: list[str] = []
    skipping = False
    for line in config.splitlines():
        section = re.match(r"^\[([^]]+)]\s*$", line)
        if section:
            skipping = section.group(1).startswith("model_providers.")
        if skipping:
            continue
        if re.match(r"^\s*(model_provider|model|model_catalog_json)\s*=", line):
            continue
        output.append(line)
    return "\n".join(output).strip()


def set_goals_feature(config: str, enabled: bool) -> str:
    output: list[str] = []
    in_features = False
    features_found = False
    goals_written = False
    for line in config.splitlines():
        section = re.match(r"^\[([^]]+)]\s*$", line)
        if section:
            if in_features and not goals_written:
                output.append(f"goals = {str(enabled).lower()}")
                goals_written = True
            in_features = section.group(1) == "features"
            features_found = features_found or in_features
            output.append(line)
            continue
        if in_features and re.match(r"^\s*goals\s*=", line):
            if not goals_written:
                output.append(f"goals = {str(enabled).lower()}")
                goals_written = True
            continue
        output.append(line)
    if not goals_written:
        if not features_found:
            if output and output[-1].strip():
                output.append("")
            output.append("[features]")
        output.append(f"goals = {str(enabled).lower()}")
    return "\n".join(output).strip()


def remove_goals_feature(config: str) -> str:
    output: list[str] = []
    in_features = False
    for line in config.splitlines():
        section = re.match(r"^\[([^]]+)]\s*$", line)
        if section:
            in_features = section.group(1) == "features"
        if in_features and re.match(r"^\s*goals\s*=", line):
            continue
        output.append(line)
    return "\n".join(output).strip()


def active_provider() -> str | None:
    if not CONFIG_PATH.exists():
        return None
    match = re.search(r'^\s*model_provider\s*=\s*"([^"]+)"', CONFIG_PATH.read_text(encoding="utf-8"), re.M)
    return match.group(1) if match else None


def public_profile(profile: dict) -> dict:
    result = profile.copy()
    result["has_bearer_token"] = bool(result.get("bearer_token"))
    result["has_auth_snapshot"] = bool(result.get("auth_contents"))
    if result.get("auth_mode") != "apikey":
        result.pop("bearer_token", None)
        result.pop("auth_contents", None)
    return result


def validate_auth_snapshot(contents: str) -> None:
    try:
        parsed = json.loads(contents)
    except json.JSONDecodeError as exc:
        raise HTTPException(422, "The captured authentication snapshot is invalid JSON") from exc
    if parsed.get("auth_mode") != "chatgpt":
        raise HTTPException(422, "The current auth.json is not a ChatGPT login session")


def api_key_from_auth_contents(contents: str) -> str | None:
    if not contents.strip():
        return None
    try:
        parsed = json.loads(contents)
    except json.JSONDecodeError as exc:
        raise HTTPException(422, "auth.json must contain valid JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(422, "auth.json must contain a JSON object")
    key = parsed.get("OPENAI_API_KEY")
    return key if isinstance(key, str) and key else None


def write_model_catalog(provider_id: str, profile: dict) -> str | None:
    models = profile.get("models", [])
    if not models:
        return None
    catalog_dir = CODEX_HOME / "model-catalogs"
    catalog_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    catalog_path = catalog_dir / f"control-panel-{provider_id}.json"
    catalog = {"models": [{"slug": item["name"], "display_name": item["name"]} for item in models]}
    write_private(catalog_path, json.dumps(catalog, ensure_ascii=False, indent=2) + "\n")
    return f"model-catalogs/{catalog_path.name}"


def config_text() -> str:
    return CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else ""


def test_profile(profile: dict) -> dict:
    endpoint = profile["base_url"].rstrip("/") + "/v1/models"
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


def test_model_request(profile: dict, test_model: str) -> dict:
    base = profile["base_url"].rstrip("/")
    wire_api = profile.get("wire_api", "responses")
    suffix = "/chat/completions" if wire_api == "chat" else "/responses"
    endpoint = f"{base[:-3] if base.endswith('/v1') else base}{suffix}"
    payload = (
        {"model": test_model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}
        if wire_api == "chat"
        else {"model": test_model, "input": "hi", "max_output_tokens": 1}
    )
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json", "Authorization": f'Bearer {profile["bearer_token"]}', "User-Agent": "CodexPlusPlus/RelayTest"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body_text = response.read().decode("utf-8", errors="replace")
            return {"ok": 200 <= response.status < 300 and bool(body_text.strip()), "endpoint": endpoint, "status": response.status, "body": body_text[:2400], "detail": "响应内容为空" if not body_text.strip() else ""}
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "endpoint": endpoint, "detail": f"HTTP {exc.code}{': ' + body_text[:1200] if body_text else ''}"}
    except urllib.error.URLError as exc:
        return {"ok": False, "endpoint": endpoint, "detail": str(exc.reason)}


def fetch_upstream_models(request: UpstreamModelFetch) -> dict:
    base = request.base_url.rstrip("/")
    candidates = [f"{base}/models"] if base.endswith("/v1") else [f"{base}/v1/models", f"{base}/models"]
    failures: list[str] = []
    for endpoint in dict.fromkeys(candidates):
        upstream_request = urllib.request.Request(
            endpoint,
            headers={"Accept": "application/json", "Authorization": f"Bearer {request.bearer_token}", "User-Agent": "CodexPlusPlus/RelayTest"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(upstream_request, timeout=12) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            failures.append(f"{endpoint}: HTTP {exc.code}")
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
        audit("upstream_models_fetched", detail=f"{len(names)} models")
        return {"endpoint": endpoint, "models": names}
    raise HTTPException(422, "Unable to fetch upstream models. Check the Base URL, API Key, and model-list endpoint.")


def diagnose_profile(profile: dict) -> dict:
    checks: list[dict[str, str]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    auth_mode = profile.get("auth_mode", "apikey")
    model = str(profile.get("model", "")).strip()
    if auth_mode == "chatgpt":
        snapshot = profile.get("auth_contents", "")
        try:
            validate_auth_snapshot(snapshot)
            add("官方认证", "pass", "已捕获有效的官方登录快照")
        except HTTPException as exc:
            add("官方认证", "fail", str(exc.detail))
    else:
        base_url = str(profile.get("base_url", "")).strip()
        api_key = str(profile.get("bearer_token") or "").strip()
        protocol = profile.get("wire_api")
        add("Base URL", "pass" if re.match(r"^https?://.+", base_url) else "fail", base_url if re.match(r"^https?://.+", base_url) else "请输入有效的 http(s) Base URL")
        add("API Key", "pass" if api_key else "fail", "已配置" if api_key else "未配置")
        add("上游协议", "pass" if protocol in {"responses", "chat"} else "fail", "Responses API" if protocol == "responses" else "Chat Completions" if protocol == "chat" else "协议无效")
        if base_url and api_key:
            try:
                upstream = fetch_upstream_models(UpstreamModelFetch(base_url=base_url, bearer_token=api_key))
                names = upstream["models"]
                add("模型目录", "pass" if names else "fail", f"{upstream['endpoint']} 返回 {len(names)} 个模型" if names else "上游没有返回可用模型")
                if model:
                    add("配置模型可见性", "pass" if model in names else "warning", "模型在上游目录中可见" if model in names else "配置模型未出现在上游模型目录；仍可能可用")
                if names:
                    test_model = str(profile.get("test_model") or model or names[0]).strip()
                    real_request = test_model_request(profile, test_model)
                    detail = f'{real_request.get("endpoint", "请求")} 返回 HTTP {real_request.get("status", "")}：{real_request.get("body", "")}' if real_request["ok"] else f'测试「{test_model}」失败：{real_request.get("endpoint", "请求")} {real_request.get("detail", "请求失败")}'
                    add("真实请求", "pass" if real_request["ok"] else "fail", detail)
                else:
                    add("真实请求", "warning", "上游未返回可测试模型，该步骤未执行")
            except HTTPException as exc:
                add("上游模型目录", "fail", str(exc.detail))
        else:
            add("模型目录", "warning", "配置不完整，该步骤未执行")
            add("真实请求", "warning", "配置不完整，该步骤未执行")
    failed = [item for item in checks if item["status"] == "fail"]
    warnings = [item for item in checks if item["status"] == "warning"]
    real_request = next((item for item in checks if item["name"] == "真实请求"), None)
    passed = not failed and not warnings and bool(real_request and real_request["status"] == "pass")
    summary = "供应商基础诊断通过。" if passed else f"发现 {len(failed)} 项失败，Codex 可能无法使用该供应商。" if failed else f"基础连接可用，但有 {len(warnings)} 项需要确认。"
    return {"ok": not failed, "passed": passed, "checks": checks, "summary": summary}


def preflight() -> dict:
    config = config_text()
    auth_keys: list[str] = []
    auth_path = CODEX_HOME / "auth.json"
    if auth_path.exists():
        try:
            auth_keys = sorted(json.loads(auth_path.read_text(encoding="utf-8")).keys())
        except json.JSONDecodeError:
            auth_keys = ["invalid JSON"]
    warnings: list[str] = []
    if re.search(r"^\s*experimental_bearer_token\s*=", config, re.M):
        warnings.append("The active config already contains a bearer token.")
    if auth_keys:
        warnings.append("auth.json contains authentication material; switching to a pure API provider will not delete it.")
    return {
        "active_provider": active_provider(),
        "auth_keys": auth_keys,
        "has_configured_bearer_token": any("bearer_token" in line for line in config.splitlines()),
        "environment_note": "Shell environment variables are not persisted by Codex and cannot be reliably inspected from this isolated console.",
        "warnings": warnings,
    }


def switch_provider(provider_id: str, verify: bool = True, model_override: str | None = None) -> dict:
    if not panel_settings()["provider_switching_enabled"]:
        raise HTTPException(409, "Provider switching is disabled in the control panel")
    profiles = read_profiles()
    profile = profiles.get(provider_id)
    if not profile:
        raise HTTPException(404, "Provider profile not found")
    auth_mode = profile.get("auth_mode", "apikey")
    diagnostic = diagnose_profile(profile) if verify else {"ok": True, "checks": [], "summary": "诊断已跳过"}
    check = {"ok": diagnostic["ok"], "detail": diagnostic["summary"], "diagnostic": diagnostic}
    if not check["ok"]:
        audit("provider_switch_rejected", provider_id=provider_id, detail=check.get("detail"))
        raise HTTPException(422, {"message": "Provider connection test failed; configuration was not changed.", "check": check})
    backup_id, backup_dir = backup_state()
    base = remove_goals_feature(remove_provider_sections(config_text()))
    if auth_mode == "apikey" and not profile.get("bearer_token"):
        raise HTTPException(422, "An API key is required for a pure API provider")
    if auth_mode == "chatgpt":
        snapshot = profile.get("auth_contents")
        if not snapshot:
            raise HTTPException(422, "Capture the current ChatGPT authentication before activating this profile")
        validate_auth_snapshot(snapshot)
    catalog_path = write_model_catalog(provider_id, profile)
    generated = [
        f'model_provider = {toml_quote(provider_id)}',
        f'model = {toml_quote(model_override or profile["model"])}',
        "",
        f'[model_providers.{provider_id}]',
        f'name = {toml_quote(profile["name"])}',
        f'requires_openai_auth = {str(auth_mode == "chatgpt").lower()}',
    ]
    if auth_mode == "apikey":
        generated.insert(5, f'wire_api = {toml_quote(profile["wire_api"])}')
    if auth_mode == "apikey" and profile.get("base_url"):
        generated.insert(5, f'base_url = {toml_quote(profile["base_url"].rstrip("/"))}')
    if profile.get("bearer_token"):
        generated.append(f'experimental_bearer_token = {toml_quote(profile["bearer_token"])}')
    if catalog_path:
        generated.insert(2, f'model_catalog_json = {toml_quote(catalog_path)}')
    provider_config = profile.get("config_contents", "").strip()
    if provider_config:
        masked_token = 'experimental_bearer_token = "***"'
        if profile.get("bearer_token"):
            provider_config = provider_config.replace(masked_token, f'experimental_bearer_token = {toml_quote(profile["bearer_token"])}')
    else:
        provider_config = "\n".join(generated)
    if profile.get("goals_configured", False):
        provider_config = set_goals_feature(provider_config, bool(profile.get("goals_enabled", False)))
    try:
        write_private(CONFIG_PATH, base + "\n\n" + provider_config.rstrip() + "\n")
        if auth_mode == "apikey":
            api_auth = profile.get("auth_contents", "").strip()
            if not api_auth:
                api_auth = json.dumps({"OPENAI_API_KEY": profile["bearer_token"]}, indent=2)
            write_private(AUTH_PATH, api_auth.rstrip() + "\n")
        else:
            write_private(AUTH_PATH, profile["auth_contents"].rstrip() + "\n")
    except OSError as exc:
        for name, target in (("config.toml", CONFIG_PATH), ("auth.json", AUTH_PATH)):
            backup_file = backup_dir / name
            if backup_file.exists():
                shutil.copy2(backup_file, target)
                os.chmod(target, 0o600)
        audit("provider_switch_failed", provider_id=provider_id, detail=str(exc))
        raise HTTPException(500, "Configuration write failed and the previous configuration was restored.") from exc
    audit("provider_switched", provider_id=provider_id, backup_id=backup_id, model=model_override or profile["model"])
    return {"active_provider": provider_id, "backup_id": backup_id, "check": check, "model": model_override or profile["model"], "auth_mode": auth_mode}


@app.get("/api/status")
def status() -> dict:
    model = None
    if CONFIG_PATH.exists():
        match = re.search(r'^\s*model\s*=\s*"([^"]+)"', CONFIG_PATH.read_text(encoding="utf-8"), re.M)
        model = match.group(1) if match else None
    return {"active_provider": active_provider(), "model": model, "profiles": [public_profile(p) for p in read_profiles().values()], "preflight": preflight(), "settings": panel_settings()}


class PanelSettings(BaseModel):
    provider_switching_enabled: bool = True
    ssh: dict = Field(default_factory=dict)
    reverse_proxy: dict = Field(default_factory=dict)


@app.get("/api/settings")
def get_settings() -> dict:
    return panel_settings()


@app.post("/api/settings")
def save_settings(request: PanelSettings) -> dict:
    settings = request.model_dump()
    write_private(SETTINGS_PATH, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
    audit("provider_switching_setting_changed", enabled=request.provider_switching_enabled)
    return settings


@app.post("/api/providers")
def save_provider(provider: Provider) -> dict:
    profiles = read_profiles()
    data = provider.model_dump()
    existing = profiles.get(provider.id, {})
    if data["auth_mode"] == "apikey":
        entered_key = data["bearer_token"]
        key_from_auth = api_key_from_auth_contents(data["auth_contents"])
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
    if not data["bearer_token"] and provider.id in profiles:
        data["bearer_token"] = profiles[provider.id].get("bearer_token")
    data["requires_openai_auth"] = data["auth_mode"] == "chatgpt"
    backup_state()
    profiles[provider.id] = data
    write_private(PROFILE_PATH, json.dumps(profiles, ensure_ascii=False, indent=2) + "\n")
    audit("provider_saved", provider_id=provider.id)
    return public_profile(data)


@app.post("/api/providers/{provider_id}/activate")
def activate_provider(provider_id: str, verify: bool = True) -> dict:
    return switch_provider(provider_id, verify=verify)


@app.delete("/api/providers/{provider_id}")
def delete_provider(provider_id: str) -> dict:
    if active_provider() == provider_id:
        raise HTTPException(409, "Switch away from the active provider before deleting it")
    profiles = read_profiles()
    if provider_id not in profiles:
        raise HTTPException(404, "Provider profile not found")
    backup_state()
    del profiles[provider_id]
    write_private(PROFILE_PATH, json.dumps(profiles, ensure_ascii=False, indent=2) + "\n")
    audit("provider_deleted", provider_id=provider_id)
    return {"deleted": provider_id}


@app.get("/api/preflight")
def get_preflight() -> dict:
    return preflight()


@app.post("/api/providers/{provider_id}/capture-auth")
def capture_chatgpt_auth(provider_id: str) -> dict:
    profiles = read_profiles()
    profile = profiles.get(provider_id)
    if not profile:
        raise HTTPException(404, "Provider profile not found")
    if profile.get("auth_mode") != "chatgpt":
        raise HTTPException(422, "Only a ChatGPT login provider can capture this auth snapshot")
    if not AUTH_PATH.exists():
        raise HTTPException(404, "auth.json was not found")
    contents = AUTH_PATH.read_text(encoding="utf-8")
    validate_auth_snapshot(contents)
    backup_state()
    profile["auth_contents"] = contents
    profiles[provider_id] = profile
    write_private(PROFILE_PATH, json.dumps(profiles, ensure_ascii=False, indent=2) + "\n")
    audit("chatgpt_auth_captured", provider_id=provider_id)
    return {"provider_id": provider_id, "captured": True}


@app.post("/api/providers/{provider_id}/test")
def test_saved_provider(provider_id: str) -> dict:
    profile = read_profiles().get(provider_id)
    if not profile:
        raise HTTPException(404, "Provider profile not found")
    result = diagnose_profile(profile)
    audit("provider_tested", provider_id=provider_id, passed=result["ok"])
    return result


@app.post("/api/providers/diagnose")
def diagnose_unsaved_provider(request: ProviderDiagnosticRequest) -> dict:
    result = diagnose_profile(request.model_dump())
    audit("provider_diagnosed", passed=result["ok"])
    return result


@app.post("/api/upstream/models")
def fetch_models_from_upstream(request: UpstreamModelFetch) -> dict:
    return fetch_upstream_models(request)


@app.get("/api/backups")
def list_backups() -> list[dict]:
    if not BACKUP_ROOT.exists():
        return []
    return [{"id": path.name, "has_config": (path / "config.toml").exists()} for path in sorted(BACKUP_ROOT.iterdir(), reverse=True) if path.is_dir()]


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    official_panel = '''<div id="official-fields" class="field-help" style="display:none"><div>官方登录档案使用当前服务器的 Codex 登录状态，不写入 API Key。</div><div style="margin-top:8px"><button id="capture-auth-btn" type="button" class="btn light small" onclick="captureOfficialAuth()">捕获当前官方登录</button> <span id="capture-auth-status" class="muted">请先保存该档案，再捕获认证快照。</span></div><div style="margin-top:7px">认证从当前部署挂载的 Codex 目录读取并私有保存；令牌不会在页面显示。</div></div>'''
    old_panel = '''<div id="official-fields" class="field-help" style="display:none">官方登录档案会使用当前服务器的 Codex 登录状态。保存档案后点击“捕获当前官方登录”建立加密前的本地认证快照；快照不会在页面显示。</div>'''
    official_script = r'''<script>
document.head.insertAdjacentHTML('beforeend','<style>#model-list.model-list-box{min-height:150px;max-height:300px;overflow:auto;border:1px solid #d2d6da;border-radius:7px;background:#fff;padding:10px 12px}.model-entry{line-height:1.8;font:14px "Consolas","Microsoft YaHei",sans-serif;color:#2c3035}.model-entry:empty{display:none}.doctor-mask{display:none;position:fixed;inset:0;background:#0008;z-index:1000;align-items:center;justify-content:center}.doctor-mask.show{display:flex}.doctor-card{width:min(560px,calc(100vw - 32px));background:#fff;border-radius:11px;padding:19px;box-shadow:0 18px 60px #0004}.doctor-title{font-size:18px;font-weight:500}.doctor-summary{margin:8px 0 12px;color:#656b73}.doctor-progress{height:8px;background:#e8ebee;border-radius:999px;overflow:hidden;margin-bottom:10px}.doctor-progress i{display:block;height:100%;width:0;background:#1683ff;border-radius:inherit;transition:width .22s}#doctor-state{padding:3px 8px;background:#f3f4f5;border-radius:999px;font-size:12px;color:#30343a}.doctor-check{min-height:64px;border:1px solid #e1e4e8;border-radius:9px;padding:12px 12px 12px 50px;margin-top:10px;position:relative}.doctor-check:before{content:"✓";position:absolute;left:15px;top:17px;width:20px;height:20px;border-radius:50%;background:#f0f1f2;color:#1683ff;display:grid;place-items:center;font-size:12px;font-weight:700}.doctor-check b{display:block}.doctor-check small{display:block;color:#6a7078;margin-top:5px;line-height:1.45}.doctor-check.real-result.expanded{height:130px;overflow:hidden}.doctor-check.real-result.expanded small{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:5;overflow:hidden}.doctor-check.running{border-color:#acd4ff}.doctor-check.running:before{content:"◌"}.doctor-check.fail:before{content:"!";color:#ed5b5b}.doctor-check.warning:before{content:"";background:#f0f1f2}.doctor-advice{margin-top:10px}.doctor-advice .doctor-check{margin-top:0}.doctor-advice .doctor-check:before{content:"◆";color:#e49a13;font-size:10px}</style>');
document.body.insertAdjacentHTML('beforeend','<div id="doctor-mask" class="doctor-mask"><div class="doctor-card"><div class="row-between"><div class="doctor-title">Provider Doctor</div><span id="doctor-state" class="section-hint"></span><button class="back" onclick="closeDoctor()">×</button></div><div id="doctor-summary" class="doctor-summary"></div><div class="doctor-progress"><i id="doctor-progress"></i></div><div id="doctor-checks"></div><div id="doctor-advice" class="doctor-advice"></div><p style="margin:16px 0 0"><button id="doctor-close" class="btn light" onclick="closeDoctor()">关闭</button></p></div></div>');
const modelList=$('#model-list');const modelHead=modelList?.previousElementSibling,modelHelp=modelHead?.previousElementSibling,modelTitle=modelHelp?.previousElementSibling;if(modelHead){modelHead.remove()}if(modelTitle){modelTitle.querySelector('button')?.remove()}if(modelList){modelList.classList.add('model-list-box');modelList.setAttribute('aria-readonly','true')}if(modelHelp)modelHelp.textContent='模型名称仅能通过“从上游获取”填入。';
function setModelListVisibility(official){const list=$('#model-list');if(!list)return;const modelHelp=list.previousElementSibling,modelTitle=modelHelp?.previousElementSibling;[list,modelHelp,modelTitle].forEach(node=>{if(node)node.style.display=official?'none':''})}
function authModeChanged(){const official=$('#p-auth').value==='chatgpt';$('#api-fields').style.display=official?'none':'grid';$('#official-fields').style.display=official?'block':'none';$('#p-url').required=!official;$('#test-btn').style.display=official?'none':'';const protocols=$('.protocols'),protocolTitle=protocols?.previousElementSibling;if(protocols)protocols.style.display=official?'none':'flex';if(protocolTitle)protocolTitle.style.display=official?'none':'';setModelListVisibility(official);if(official){const captured=state.current?.has_auth_snapshot;$('#capture-auth-status').textContent=captured?'已捕获服务器官方登录快照。':'请先保存该档案，再捕获认证快照。';$('#capture-auth-btn').disabled=!state.current?.id}updatePreview()}
async function captureOfficialAuth(){try{if($('#p-auth').value!=='chatgpt')throw Error('仅官方登录档案可捕获认证。');await saveProvider();const p=gather();if(!p.id)throw Error('请先填写并保存供应商名称。');const d=await api(`/api/providers/${encodeURIComponent(p.id)}/capture-auth`,{method:'POST'});state.current={...p,has_auth_snapshot:d.captured};$('#capture-auth-btn').disabled=false;$('#capture-auth-status').textContent='已捕获服务器官方登录快照。';note('已捕获服务器官方登录；认证令牌不会显示。');await refreshAll()}catch(e){note(e.message)}}
async function fetchModelsFromUpstream(){try{if($('#p-auth').value!=='apikey')throw Error('从上游获取仅适用于纯 API 供应商。');const base_url=$('#p-url').value.trim(),bearer_token=$('#p-key').value;if(!base_url||!bearer_token)throw Error('请先填写 Base URL 和 Key。');const button=$('#fetch-models-btn');button.disabled=true;button.textContent='正在获取…';const result=await api('/api/upstream/models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({base_url,bearer_token})});$('#model-list').innerHTML='';result.models.forEach(name=>addModel({name}));if(!state.testModel&&result.models.length)state.testModel=result.models[0];note(`已从上游获取 ${result.models.length} 个模型。`);updatePreview()}catch(e){note(e.message)}finally{const button=$('#fetch-models-btn');if(button){button.disabled=false;button.textContent='⇩ 从上游获取'}}}
function installFetchModelsButton(){const list=$('#model-list');if(!list||$('#fetch-models-btn'))return;const modelHelp=list.previousElementSibling,modelTitle=modelHelp?.previousElementSibling;if(!modelTitle)return;const button=document.createElement('button');button.id='fetch-models-btn';button.type='button';button.className='btn light small';button.textContent='⇩ 从上游获取';button.onclick=fetchModelsFromUpstream;modelTitle.append(button)}
installFetchModelsButton()
function newProvider(){state.current={id:'',name:'',base_url:'',model:'',wire_api:'responses',auth_mode:'chatgpt',models:[],goals_enabled:false,goals_configured:false,test_model:''};openDetail()}
const migrationTarget=$('#migration-target');if(migrationTarget){const migrationPanel=migrationTarget.parentElement;const migrationSection=migrationPanel?.parentElement;migrationPanel?.remove();if(migrationSection)migrationSection.style.gridTemplateColumns='1fr'}
const commonConfig=$('#common-config');if(commonConfig){const commonPanel=commonConfig.parentElement;const commonSection=commonPanel?.parentElement;commonPanel?.remove();if(commonSection)commonSection.style.gridTemplateColumns='1fr'}
const routeModel=$('#route-model');if(routeModel){const routeRow=routeModel.parentElement;const routeHelp=routeRow?.previousElementSibling;const routeTitle=routeHelp?.previousElementSibling;routeRow?.remove();routeHelp?.remove();routeTitle?.remove()}
async function refreshRoutes(){}
function addModel(m={name:''}){if(!m.name)return;const entry=document.createElement('div');entry.className='model-entry';entry.dataset.name=m.name;entry.textContent=m.name;$('#model-list').append(entry)}
function openDetail(){const p=state.current;$('#list-view').classList.add('hidden');$('#detail').classList.add('visible');$('#detail-name').textContent=p.id?p.name:'添加供应商';$('#detail-sub').textContent=p.id===state.active?'当前正在使用':p.id?'编辑后保存列表，再切换模式时会使用新配置':'新建供应商需要先保存到列表';$('#activate-btn').style.display=p.id?'':'none';$('#p-name').value=p.name||'';$('#p-model').value=p.model||'';$('#p-url').value=p.base_url||'';$('#p-key').value=p.auth_mode==='apikey'?(p.bearer_token||''):'';$('#p-auth').value=p.auth_mode||'apikey';$('#auth-preview').value=p.auth_mode==='apikey'?(p.auth_contents||''):'';$('#p-goals').checked=Boolean(p.goals_enabled);state.testModel=p.test_model||'';state.goalsConfigured=Boolean(p.goals_configured);state.protocol=p.wire_api||'responses';state.configTouched=Boolean(p.config_contents);$('#config-preview').value=p.config_contents||'';setProtocol(state.protocol);$('#model-list').innerHTML='';(p.models||[]).forEach(addModel);authModeChanged();if(state.goalsConfigured)syncGoalsConfig();updatePreview()}
function gather(){const id=(state.current?.id||$('#p-name').value.trim().toLowerCase().replace(/[^a-z0-9_-]+/g,'-')).replace(/^-+|-+$/g,'');const auth_mode=$('#p-auth').value;return {id,name:$('#p-name').value.trim(),base_url:$('#p-url').value.trim(),model:$('#p-model').value.trim(),wire_api:state.protocol,auth_mode,bearer_token:$('#p-key').value,models:[...document.querySelectorAll('.model-entry')].map(entry=>({name:entry.dataset.name})).filter(m=>m.name),config_contents:$('#config-preview').value,auth_contents:auth_mode==='apikey'?$('#auth-preview').value:'',goals_enabled:$('#p-goals').checked,goals_configured:Boolean(state.goalsConfigured),test_model:state.testModel||''}}
function updatePreview(){if(!state.current)return;const p=gather();if(p.auth_mode==='apikey'){if(!$('#auth-preview').value.trim())$('#auth-preview').value=JSON.stringify({OPENAI_API_KEY:$('#p-key').value},null,2)}else $('#auth-preview').value=''}
function syncKeyToAuth(){if($('#p-auth').value==='apikey')$('#auth-preview').value=JSON.stringify({OPENAI_API_KEY:$('#p-key').value},null,2)}
function syncAuthToKey(){if($('#p-auth').value!=='apikey')return;try{const value=JSON.parse($('#auth-preview').value);if(value&&typeof value.OPENAI_API_KEY==='string')$('#p-key').value=value.OPENAI_API_KEY}catch{}}
function syncGoalsConfig(){state.goalsConfigured=true;const text=$('#config-preview'),enabled=$('#p-goals').checked,goal=`goals = ${enabled?'true':'false'}`;let value=text.value;if(/^\[features\]\s*$/m.test(value)){if(/^\s*goals\s*=/m.test(value))value=value.replace(/^\s*goals\s*=.*$/m,goal);else value=value.replace(/^(\[features\]\s*)$/m,`$1\n${goal}`)}else value=(value.trim()?value.trim()+'\n\n':'')+`[features]\n${goal}`;text.value=value;state.configTouched=true}
$('#p-key').addEventListener('input',syncKeyToAuth);$('#auth-preview').addEventListener('input',syncAuthToKey);
function closeDoctor(){$('#doctor-mask').classList.remove('show')}
function doctorStatus(items){return items.some(item=>item.status==='fail')?'fail':items.some(item=>item.status==='warning')?'warning':'pass'}
function showDoctorProgress(){const stages=[['配置完整性','正在检查配置完整性…'],['模型列表','等待检查 /v1/models…'],['真实请求','等待发送一次测试请求…'],['处理建议','等待生成处理建议。']];let step=0;$('#doctor-state').textContent='诊断中';$('#doctor-summary').textContent='正在诊断供应商，请稍候。';$('#doctor-progress').style.width='18%';$('#doctor-checks').innerHTML=stages.map(([name,detail],index)=>`<div class="doctor-check ${index===0?'running':''}"><b>${name}</b><small>${detail}</small></div>`).join('');$('#doctor-advice').textContent='诊断中';$('#doctor-close').style.display='none';$('#doctor-mask').classList.add('show');return setInterval(()=>{step=Math.min(step+1,3);$('#doctor-progress').style.width=`${18+step*19}%`;[...document.querySelectorAll('#doctor-checks .doctor-check')].forEach((node,index)=>node.classList.toggle('running',index===step))},360)}
function showDoctor(result){const all=result.checks||[];const configuration=all.filter(item=>['Base URL','API Key','上游协议','官方认证'].includes(item.name));const models=all.filter(item=>item.name.includes('模型目录')||item.name==='配置模型可见性');const real=all.filter(item=>item.name==='真实请求');const groups=[['配置完整性',configuration],['模型列表',models],['真实请求',real]];$('#doctor-state').textContent=result.passed?'完成':result.ok?'待确认':'异常';$('#doctor-summary').textContent=result.summary;$('#doctor-progress').style.width='100%';$('#doctor-checks').innerHTML=groups.map(([name,items])=>{const status=doctorStatus(items),detail=items.length?items.map(item=>item.detail).join('；'):'该步骤未执行。';const expanded=name==='真实请求'&&items.some(item=>item.status==='pass');return `<div class="doctor-check ${status} ${expanded?'real-result expanded':''}"><b>${esc(name)}</b><small>${esc(detail)}</small></div>`}).join('');const failed=all.filter(item=>item.status==='fail'),modelWarning=all.find(item=>item.name==='配置模型可见性'&&item.status==='warning'),modelFailure=all.find(item=>item.name==='模型目录'||item.name==='上游模型目录'),realFailure=all.find(item=>item.name==='真实请求'&&item.status==='fail');const advice=result.passed?'可以作为 Codex 供应商使用；如果真实对话仍失败，请查看协议代理日志里的上游响应。':failed.some(item=>item.name==='Base URL'||item.name==='API Key')?'先补齐 Base URL 和 API Key；如果使用官方账号，请切换到官方登录模式。':modelWarning?'连接可用，但测试模型没有出现在模型列表里；建议改用上游返回的模型名。':modelFailure?.status==='fail'?'优先检查 Base URL 是否包含正确的 /v1 前缀，以及供应商是否支持 /v1/models。':realFailure?'优先检查测试模型名称、上游协议选择和 Key 权限；如果 Chat Completions 可用，请切到对应协议。':'请检查上游服务配置。';$('#doctor-advice').innerHTML=`<div class="doctor-check"><b>处理建议</b><small>${esc(advice)}</small></div>`;$('#doctor-close').style.display='';$('#doctor-mask').classList.add('show')}
async function testCurrent(){const timer=showDoctorProgress();try{const [d]=await Promise.all([api('/api/providers/diagnose',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(gather())}),new Promise(resolve=>setTimeout(resolve,450))]);clearInterval(timer);showDoctor(d)}catch(e){clearInterval(timer);$('#doctor-state').textContent='异常';$('#doctor-summary').textContent='诊断请求失败。';$('#doctor-progress').style.width='100%';$('#doctor-checks').innerHTML=`<div class="doctor-check fail"><b>诊断错误</b><small>${esc(e.message)}</small></div>`;$('#doctor-advice').textContent='请检查控制台服务、网络连接和上游配置。';$('#doctor-close').style.display='';}}
 </script>'''
    navigation_script = r'''<script>
 document.head.insertAdjacentHTML('beforeend', `<style>
 .console-sidebar{position:fixed;inset:0 auto 0 0;width:228px;background:#202124;color:#f7f8fa;padding:22px 14px;z-index:20;display:flex;flex-direction:column;gap:22px}.console-brand{font-size:17px;font-weight:750;padding:0 12px}.console-brand small{display:block;color:#aeb4bb;font-size:11px;font-weight:400;margin-top:5px}.console-nav{display:grid;gap:5px}.console-nav button{border:0;background:transparent;color:#cfd3d8;text-align:left;border-radius:7px;padding:11px 12px;font:inherit;cursor:pointer}.console-nav button:hover,.console-nav button.active{background:#34373b;color:#fff}.console-content{margin-left:228px}.console-panel{max-width:1320px;margin:22px auto;padding:0 26px}.console-panel .list-shell{background:#fff;border:1px solid #dde1e6;border-radius:11px;padding:18px}.console-panel h2{margin:0 0 7px;font-size:18px}.console-panel .panel-note{color:#686e76;font-size:13px;margin:0 0 18px}.console-form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.console-form label{display:block;color:#555c64;font-size:12px;margin-bottom:5px}.console-form input,.console-form select,.console-form textarea{width:100%;box-sizing:border-box;padding:9px 10px;border:1px solid #d2d6da;border-radius:7px;font:inherit;background:#fff}.console-form textarea{min-height:104px;resize:vertical}.console-form .wide{grid-column:1/-1}.console-form-actions{display:flex;gap:8px;margin-top:17px}.console-code{font:12px Consolas,monospace;background:#f4f5f6;color:#30343a;padding:12px;border-radius:7px;white-space:pre-wrap;overflow:auto}.console-muted{color:#727982;font-size:12px}
 @media(max-width:800px){.console-sidebar{width:190px}.console-content{margin-left:190px}.console-form{grid-template-columns:1fr}}
 </style>`);
 document.body.insertAdjacentHTML('afterbegin', `<aside class="console-sidebar"><div class="console-brand">Codex 控制台<small>通用服务器管理</small></div><nav class="console-nav"><button data-section="providers" onclick="openConsoleSection('providers')">供应商配置</button><button data-section="ssh" onclick="openConsoleSection('ssh')">SSH 连接</button><button data-section="proxy" onclick="openConsoleSection('proxy')">反向代理</button></nav></aside>`);
 document.querySelector('.top')?.classList.add('console-content');document.querySelectorAll('.page').forEach(e=>e.classList.add('console-content'));
 document.body.insertAdjacentHTML('beforeend', `<section id="console-ssh" class="console-panel console-nav-panel" style="display:none"><div class="list-shell"><h2>SSH 连接</h2><p class="panel-note">保存连接参数并生成 SSH 隧道命令。私钥内容不会上传或保存。</p><div class="console-form"><div><label>服务器地址</label><input id="ssh-host" placeholder="例如 203.0.113.10"></div><div><label>SSH 端口</label><input id="ssh-port" type="number" value="22"></div><div><label>用户名</label><input id="ssh-user" placeholder="例如 ubuntu"></div><div><label>本地访问端口</label><input id="ssh-local-port" type="number" value="8787"></div><div class="wide"><label>隧道命令</label><div id="ssh-command" class="console-code">填写服务器地址和用户名后生成</div><div class="console-muted">执行后，在本机打开 http://127.0.0.1:8787</div></div></div><div class="console-form-actions"><button class="btn" onclick="saveConsoleSettings()">保存 SSH 配置</button></div><div id="ssh-notice" class="notice"></div></div></section><section id="console-proxy" class="console-panel console-nav-panel" style="display:none"><div class="list-shell"><h2>反向代理</h2><p class="panel-note">为域名访问生成 Nginx 配置片段。建议启用 HTTPS 和访问认证后再公开服务。</p><div class="console-form"><div><label>域名</label><input id="proxy-domain" placeholder="console.example.com"></div><div><label>上游地址</label><input id="proxy-upstream" value="127.0.0.1:8787"></div><div><label>TLS 证书路径</label><input id="proxy-cert" placeholder="/etc/letsencrypt/live/example/fullchain.pem"></div><div><label>TLS 私钥路径</label><input id="proxy-key" placeholder="/etc/letsencrypt/live/example/privkey.pem"></div><div class="wide"><label>Nginx 配置预览</label><pre id="proxy-config" class="console-code">填写域名后生成</pre></div></div><div class="console-form-actions"><button class="btn" onclick="saveConsoleSettings()">保存反向代理配置</button></div><div id="proxy-notice" class="notice"></div></div></section>`);
 function openConsoleSection(section){document.querySelectorAll('.console-nav button').forEach(b=>b.classList.toggle('active',b.dataset.section===section));document.querySelectorAll('.console-nav-panel').forEach(p=>p.style.display='none');const list=document.querySelector('#list-view'),detail=document.querySelector('#detail');if(section==='providers'){if(list)list.style.display='';if(detail&&detail.classList.contains('visible'))detail.style.display='';}else{if(list)list.style.display='none';if(detail)detail.style.display='none';document.querySelector('#console-'+(section==='ssh'?'ssh':'proxy')).style.display='block'}localStorage.setItem('console-section',section)}
 function updateSshCommand(){const host=$('#ssh-host')?.value.trim(),user=$('#ssh-user')?.value.trim(),port=$('#ssh-port')?.value||22,local=$('#ssh-local-port')?.value||8787;$('#ssh-command').textContent=host&&user?`ssh -N -L ${local}:127.0.0.1:8787 -p ${port} ${user}@${host}`:'填写服务器地址和用户名后生成'}
 function updateProxyConfig(){const domain=$('#proxy-domain')?.value.trim(),upstream=$('#proxy-upstream')?.value.trim()||'127.0.0.1:8787',cert=$('#proxy-cert')?.value.trim(),key=$('#proxy-key')?.value.trim();$('#proxy-config').textContent=domain?`server {\n    listen 443 ssl;\n    server_name ${domain};\n    ssl_certificate ${cert||'/path/to/fullchain.pem'};\n    ssl_certificate_key ${key||'/path/to/privkey.pem'};\n    location / {\n        proxy_pass http://${upstream};\n        proxy_set_header Host $host;\n        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n    }\n}`:'填写域名后生成'}
 ['ssh-host','ssh-port','ssh-user','ssh-local-port'].forEach(id=>document.getElementById(id)?.addEventListener('input',updateSshCommand));['proxy-domain','proxy-upstream','proxy-cert','proxy-key'].forEach(id=>document.getElementById(id)?.addEventListener('input',updateProxyConfig));
 async function loadConsoleSettings(){try{const s=await api('/api/settings');const ssh=s.ssh||{},proxy=s.reverse_proxy||{};for(const [id,key] of [['ssh-host','host'],['ssh-port','port'],['ssh-user','user'],['ssh-local-port','local_port']])if(ssh[key]!=null)$('#'+id).value=ssh[key];for(const [id,key] of [['proxy-domain','domain'],['proxy-upstream','upstream'],['proxy-cert','cert'],['proxy-key','key']])if(proxy[key]!=null)$('#'+id).value=proxy[key];updateSshCommand();updateProxyConfig()}catch{}}
 async function saveConsoleSettings(){try{const s=await api('/api/settings'),payload={provider_switching_enabled:s.provider_switching_enabled,ssh:{host:$('#ssh-host').value.trim(),port:$('#ssh-port').value||22,user:$('#ssh-user').value.trim(),local_port:$('#ssh-local-port').value||8787},reverse_proxy:{domain:$('#proxy-domain').value.trim(),upstream:$('#proxy-upstream').value.trim()||'127.0.0.1:8787',cert:$('#proxy-cert').value.trim(),key:$('#proxy-key').value.trim()}};await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});note('连接配置已保存。',document.querySelector('#console-ssh').style.display==='none'?'proxy-notice':'ssh-notice')}catch(e){note(e.message)}}
 loadConsoleSettings();openConsoleSection(localStorage.getItem('console-section')||'providers');
 </script>'''
    return (
        NEW_HTML.replace(old_panel, official_panel)
        .replace('<button class="btn outline" onclick="loadCommon()">通用配置</button>', "")
        .replace('<button class="btn" onclick="restartHint()">重启 Codex</button>', "")
        .replace('<button class="btn light" onclick="loadCommon()">提取通用配置</button>', "")
        .replace('<button class="btn light" onclick="newProvider(\'apikey\')">＋ 添加供应商</button><button class="btn light" onclick="newProvider(\'chatgpt\')">＋ 添加官方登录供应商</button>', '<button class="btn light" onclick="newProvider()">＋ 添加供应商</button>')
        .replace('每行一个模型；上下文窗口和图片处理方式将一并保存到供应商档案。', '每行一个模型名称；可手动输入，或从上游获取后自动填入。')
        .replace('<div class="model-head"><span>模型名称</span><span>上下文窗口</span><span>图片处理方式</span><span></span></div>', '<div class="model-head"><span>模型名称</span><span></span></div>')
        .replace('<div class="preview-note">切换到此供应商时会写入的预览；不会显示密钥。</div><textarea id="config-preview" class="config-area" readonly>', '<div class="preview-note">可手动补充或修改；切换到此供应商时会写入，密钥以掩码保存。</div><textarea id="config-preview" class="config-area" oninput="state.configTouched=true">')
        .replace('<textarea id="auth-preview" class="config-area" readonly>', '<textarea id="auth-preview" class="config-area">')
        .replace('<input id="p-key" type="password" placeholder="保存后不再显示">', '<input id="p-key" type="text" placeholder="API Key">')
        .replace('placeholder="例如 fhl"', 'placeholder="例如 chatgpt"')
        .replace('<input id="p-model" value="gpt-5.6-terra" placeholder="例如 gpt-5.6-terra" oninput="updatePreview()">', '<input id="p-model" placeholder="例如 gpt-5.6-terra" oninput="updatePreview()">')
        .replace('<div class="field"><label>Codex 目标</label><select id="p-target"><option value="">不启用目标功能</option></select></div>', '<div class="field"><label>Codex 目标</label><label style="display:flex;align-items:center;gap:8px;border:1px solid #d2d6da;border-radius:7px;padding:10px 12px;font-weight:400"><input id="p-goals" type="checkbox" onchange="syncGoalsConfig()" style="width:auto">启用目标功能</label></div>')
         .replace("</body></html>", official_script + navigation_script + "</body></html>")
    )


NEW_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>供应商配置 · Codex</title>
<style>
:root{--line:#e5e7eb;--text:#24272b;--muted:#686e76;--bg:#f7f8fa;--blue:#1683ff;--blue-fill:#e8f3ff;--black:#202124}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px "Microsoft YaHei","PingFang SC",Arial,sans-serif}.top{height:66px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 27px}.title{font-size:21px;font-weight:750;letter-spacing:-.4px}.subtitle{font-size:12px;color:#5f666e;margin-top:3px}.head-actions{display:flex;gap:8px}.btn{border:0;border-radius:7px;padding:8px 13px;background:var(--black);color:#fff;font:inherit;font-weight:650;cursor:pointer;white-space:nowrap}.btn.light{background:#f0f1f2;color:#30343a}.btn.outline{background:#fff;color:#30343a;border:1px solid #d7d9dd}.btn.primary{background:var(--black)}.btn.small{padding:6px 10px;font-size:13px}.page{max-width:1320px;margin:22px auto;padding:0 26px}.list-shell{background:#fff;border:1px solid #dde1e6;border-radius:11px;padding:15px}.section-title{font-size:16px;font-weight:750}.section-hint{color:var(--muted);font-size:13px}.row-between{display:flex;align-items:center;justify-content:space-between;gap:16px}.switch-panel{border:1px solid #dce0e5;border-radius:10px;padding:15px;margin:15px 0 12px;display:flex;align-items:center;justify-content:space-between}.switch-title{font-weight:700}.switch-note{font-size:12px;color:#676d76;margin-top:5px}.toggle{width:38px;height:24px;background:#aeb4bb;border-radius:999px;padding:3px;cursor:pointer;border:0}.toggle:after{content:"";display:block;width:18px;height:18px;border-radius:50%;background:white;transition:.18s}.toggle.on{background:var(--blue)}.toggle.on:after{transform:translateX(14px)}.toolbar{display:flex;justify-content:flex-end;gap:8px;margin:10px 0}.provider-card{display:flex;gap:16px;align-items:center;border:1px solid #dfe2e6;border-radius:9px;padding:15px 17px;margin-top:8px;background:#fff;cursor:pointer}.provider-card.active{background:var(--blue-fill);border-color:#58a8ff}.handle{color:#858b93;font-weight:bold;letter-spacing:1px}.badge{width:31px;height:31px;border-radius:9px;background:#e8eaee;display:grid;place-items:center;font-weight:750;color:#565b63}.provider-card.active .badge{background:#d2eaff;color:#137de8}.card-name{font-weight:750}.card-meta{font-size:12px;color:#127df1;margin-top:7px}.empty{padding:22px;color:var(--muted);text-align:center}.detail{display:none}.detail.visible{display:block}.list-view.hidden{display:none}.detail-top{height:59px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 32px}.back{border:0;background:transparent;color:#30343a;font:inherit;font-size:18px;cursor:pointer;padding:6px}.detail-name{font-weight:750;font-size:16px}.detail-sub{font-size:12px;color:#676d76;margin-top:4px}.detail-main{max-width:1320px;margin:18px auto;padding:0 26px}.form-card{background:#fff;border:1px solid #dfe3e7;border-radius:10px;padding:16px}.field-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px 16px}.field label{display:block;font-size:13px;font-weight:700;margin-bottom:7px}.field input,.field select,.field textarea,.config-area{width:100%;border:1px solid #d2d6da;border-radius:7px;background:#fff;padding:10px 12px;font:14px "Consolas","Microsoft YaHei",sans-serif;color:#2c3035}.field textarea{resize:vertical;min-height:94px}.field-help{font-size:12px;color:#6a7078;margin-top:6px;line-height:1.5}.subheading{font-weight:750;margin:20px 0 10px}.protocols{display:flex;gap:8px}.protocol{flex:1;padding:10px;border:1px solid #d8dde2;background:#fff;border-radius:8px;text-align:center;font-weight:650;cursor:pointer}.protocol.selected{background:var(--blue-fill);border-color:#3f9bff}.model-head,.model-row{display:grid;grid-template-columns:1.55fr .68fr .92fr 35px;gap:8px;align-items:center}.model-head{font-size:12px;font-weight:700;color:#656b73;margin:12px 0 7px}.model-row{margin:8px 0}.model-row input,.model-row select{width:100%;border:1px solid #d2d6da;border-radius:7px;padding:9px;font:inherit}.remove-model{border:0;background:transparent;color:#626871;font-size:19px;cursor:pointer}.below{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:20px}.preview-title{font-size:15px;font-weight:750;margin-bottom:5px}.preview-note{font-size:12px;line-height:1.45;color:#666d75;min-height:33px}.config-area{height:230px;resize:vertical;white-space:pre;font-size:12px;line-height:1.55;background:#fff}.notice{display:none;margin:14px 0;border:1px solid #f0cf85;background:#fff7df;color:#69521c;padding:11px 13px;border-radius:8px}.notice.show{display:block}.utility{margin-top:18px;background:#fff;border:1px solid #dfe3e7;border-radius:10px;padding:16px}.utility summary{font-weight:750;cursor:pointer}.utility-body{margin-top:15px}.route-row{display:grid;grid-template-columns:1fr 1fr auto auto;gap:8px;margin-top:9px}.muted{color:var(--muted);font-size:13px}@media(max-width:760px){.top{padding:0 14px}.page,.detail-main{padding:0 13px}.field-grid,.below{grid-template-columns:1fr}.switch-panel{align-items:flex-start}.model-head,.model-row{grid-template-columns:1fr .55fr .75fr 28px}.head-actions .outline{display:none}.route-row{grid-template-columns:1fr 1fr}.detail-top{padding:0 13px}.toolbar{flex-wrap:wrap}}
</style></head>
<body>
<header class="top"><div><div class="title">供应商配置</div><div class="subtitle">管理 API 供应商、协议、Key 与配置文件</div></div><div class="head-actions"><button class="btn outline" onclick="refreshAll()">刷新</button><button class="btn outline" onclick="loadCommon()">通用配置</button><button class="btn" onclick="restartHint()">重启 Codex</button></div></header>
<main id="list-view" class="page list-view"><section class="list-shell"><div class="row-between"><div class="section-title">供应商列表</div><div id="list-count" class="section-hint">正在读取…</div></div><div class="switch-panel"><div><div class="switch-title">启用供应商配置切换</div><div class="switch-note">关闭后此工具不会在手动切换时写入 Codex 的 config.toml / auth.json。</div></div><button id="switch" class="toggle on" aria-label="启用供应商配置切换" onclick="toggleSwitch()"></button></div><div class="toolbar"><button class="btn light" onclick="newProvider('apikey')">＋ 添加供应商</button><button class="btn light" onclick="newProvider('chatgpt')">＋ 添加官方登录供应商</button><button class="btn light" onclick="loadCommon()">提取通用配置</button></div><div id="provider-list"></div></section><div id="list-notice" class="notice"></div></main>
<section id="detail" class="detail"><div class="detail-top"><div class="row-between" style="justify-content:flex-start"><button class="back" onclick="closeDetail()">←</button><div><div id="detail-name" class="detail-name">添加供应商</div><div id="detail-sub" class="detail-sub">编辑后保存列表，再切换模式时会使用新配置</div></div></div><div class="head-actions"><button id="test-btn" class="btn light" onclick="testCurrent()">诊断供应商</button><button id="activate-btn" class="btn" onclick="activateCurrent()">设为当前</button><button class="btn" onclick="saveProvider()">保存</button></div></div><main class="detail-main"><div id="detail-notice" class="notice"></div><section class="form-card"><div class="field-grid"><div class="field"><label>名称</label><input id="p-name" placeholder="例如 fhl" oninput="updatePreview()"></div><div class="field"><label>接入模式</label><select id="p-auth" onchange="authModeChanged()"><option value="chatgpt">官方登录</option><option value="apikey">纯 API</option></select></div><div class="field"><label>配置模型</label><input id="p-model" value="gpt-5.6-terra" placeholder="例如 gpt-5.6-terra" oninput="updatePreview()"><div class="field-help">默认启动 Codex 时使用的模型名称。</div></div><div class="field"><label>Codex 目标</label><select id="p-target"><option value="">不启用目标功能</option></select></div></div><div class="subheading">更多选项</div><div id="api-fields" class="field-grid"><div class="field"><label>Base URL</label><input id="p-url" type="url" placeholder="https://api.example.com" oninput="updatePreview()"></div><div class="field"><label>Key</label><input id="p-key" type="password" placeholder="保存后不再显示"></div></div><div id="official-fields" class="field-help" style="display:none">官方登录档案会使用当前服务器的 Codex 登录状态。保存档案后点击“捕获当前官方登录”建立加密前的本地认证快照；快照不会在页面显示。</div><div class="subheading">上游协议</div><div class="protocols"><button id="responses-tab" class="protocol selected" onclick="setProtocol('responses')">Responses API</button><button id="chat-tab" class="protocol" onclick="setProtocol('chat')">Chat Completions</button></div><div class="subheading row-between"><span>模型列表</span><button class="btn light small" onclick="addModel()">＋ 添加模型</button></div><div class="field-help">每行一个模型；上下文窗口和图片处理方式将一并保存到供应商档案。</div><div class="model-head"><span>模型名称</span><span>上下文窗口</span><span>图片处理方式</span><span></span></div><div id="model-list"></div><div class="subheading">单模型路由</div><div class="field-help">快捷切换会应用对应供应商的 URL、Key 与模型；不会成为请求级的聚合代理。</div><div class="route-row"><input id="route-model" class="field-input" placeholder="模型名"><select id="route-provider"></select><button class="btn light" onclick="saveRoute()">添加模型路由</button><button class="btn light" onclick="refreshRoutes()">查看路由</button></div></section><section class="below"><div><div class="preview-title">config.toml 预览</div><div class="preview-note">切换到此供应商时会写入的预览；不会显示密钥。</div><textarea id="config-preview" class="config-area" readonly></textarea></div><div><div class="row-between"><div><div class="preview-title">通用配置文件</div><div class="preview-note">保留非 MCP、Skills、Plugins 的跨供应商配置。</div></div><button class="btn light small" onclick="extractCommon()">提取当前配置</button></div><textarea id="common-config" class="config-area" placeholder="尚未设置通用配置"></textarea><p><button class="btn light small" onclick="saveCommon()">保存通用配置</button></p></div></section><section class="below"><div><div class="preview-title">auth.json</div><div class="preview-note">密钥和令牌均以掩码显示。</div><textarea id="auth-preview" class="config-area" readonly></textarea></div><div><div class="preview-title">会话历史标签迁移</div><div class="preview-note">只更新 session_meta 的供应商标记，历史消息不会删除。</div><select id="migration-target" class="field-input"></select><input id="migration-source" class="field-input" placeholder="来源供应商（可留空）" style="margin-top:8px"><p><button class="btn light small" onclick="previewMigration()">预览</button> <button class="btn small" onclick="applyMigration()">执行迁移</button></p><div id="migration-result" class="muted"></div></div></section></main></section>
<script>
const $=s=>document.querySelector(s);let state={profiles:[],active:null,current:null,enabled:true,protocol:'responses'};
async function api(url,opt={}){const r=await fetch(url,opt);let d;try{d=await r.json()}catch{d={}}if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'请求失败');return d}
function note(text,where='detail-notice'){const e=$('#'+where);e.textContent=text;e.classList.add('show');setTimeout(()=>e.classList.remove('show'),5000)}
function esc(x){return String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function refreshAll(){const d=await api('/api/status');state.profiles=d.profiles;state.active=d.active_provider;state.enabled=d.settings?.provider_switching_enabled??true;$('#switch').classList.toggle('on',state.enabled);renderList();populateSelectors();await loadCommon();await refreshRoutes();}
function renderList(){const profiles=state.profiles;$('#list-count').textContent=`${profiles.length} 个供应商配置；可拖动排序，点击编辑进入详情`;$(' #provider-list'.trim()).innerHTML=profiles.length?profiles.map(p=>`<div class="provider-card ${p.id===state.active?'active':''}" onclick="openProvider('${esc(p.id)}')"><div class="handle">⋮⋮</div><div class="badge">${esc((p.name||p.id).slice(0,1).toUpperCase())}</div><div><div class="card-name">${esc(p.name)} ${p.id===state.active?'<span class="section-hint">使用中</span>':''}</div><div class="card-meta">${p.auth_mode==='chatgpt'?'官方登录':'纯 API'} · ${p.wire_api==='responses'?'Responses API':'Chat Completions'} · ${esc(p.base_url||'不写入 API 文件')}</div></div></div>`).join(''):'<div class="empty">尚未添加供应商。可先添加 API 供应商或官方登录档案。</div>'}
function populateSelectors(){const o=state.profiles.map(p=>`<option value="${esc(p.id)}">${esc(p.name)} (${esc(p.id)})</option>`).join('');$('#route-provider').innerHTML=o;$('#migration-target').innerHTML=o}
function newProvider(mode='apikey'){state.current={id:'',name:'',base_url:'',model:'gpt-5.6-terra',wire_api:'responses',auth_mode:mode,models:[]};openDetail()}
function openProvider(id){const p=state.profiles.find(x=>x.id===id);if(!p)return;state.current=structuredClone(p);openDetail()}
function openDetail(){const p=state.current;$('#list-view').classList.add('hidden');$('#detail').classList.add('visible');$('#detail-name').textContent=p.id? p.name:'添加供应商';$('#detail-sub').textContent=p.id===state.active?'当前正在使用':'编辑后保存列表，再切换模式时会使用新配置';$('#p-name').value=p.name||'';$('#p-model').value=p.model||'gpt-5.6-terra';$('#p-url').value=p.base_url||'';$('#p-key').value='';$('#p-auth').value=p.auth_mode||'apikey';state.protocol=p.wire_api||'responses';setProtocol(state.protocol);$('#model-list').innerHTML='';(p.models?.length?p.models:[{name:p.model||'',context_window:'1M',image_mode:'send-as-is'}]).forEach(addModel);authModeChanged();updatePreview()}
function closeDetail(){$('#detail').classList.remove('visible');$('#list-view').classList.remove('hidden');state.current=null;refreshAll()}
function authModeChanged(){const official=$('#p-auth').value==='chatgpt';$('#api-fields').style.display=official?'none':'grid';$('#official-fields').style.display=official?'block':'none';$('#p-url').required=!official;updatePreview()}
function setProtocol(v){state.protocol=v;$('#responses-tab').classList.toggle('selected',v==='responses');$('#chat-tab').classList.toggle('selected',v==='chat');updatePreview()}
function addModel(m={name:'',context_window:'1M',image_mode:'send-as-is'}){const row=document.createElement('div');row.className='model-row';row.innerHTML=`<input placeholder="例如 gpt-5.6-terra" value="${esc(m.name)}" oninput="updatePreview()"><input placeholder="1M" value="${esc(m.context_window||'')}" oninput="updatePreview()"><select onchange="updatePreview()"><option value="send-as-is">send-as-is</option><option value="omit">omit</option></select><button class="remove-model" title="删除模型" onclick="this.parentElement.remove();updatePreview()">×</button>`;row.querySelector('select').value=m.image_mode||'send-as-is';$('#model-list').append(row)}
function gather(){const id=(state.current?.id||$('#p-name').value.trim().toLowerCase().replace(/[^a-z0-9_-]+/g,'-')).replace(/^-+|-+$/g,'');return {id,name:$('#p-name').value.trim(),base_url:$('#p-url').value.trim(),model:$('#p-model').value.trim(),wire_api:state.protocol,auth_mode:$('#p-auth').value,bearer_token:$('#p-key').value,models:[...document.querySelectorAll('.model-row')].map(r=>({name:r.children[0].value.trim(),context_window:r.children[1].value.trim(),image_mode:r.children[2].value})).filter(m=>m.name)}}
function updatePreview(){if(!state.current)return;const p=gather();const lines=[`model = "${p.model||'gpt-5.6-terra'}"`,`model_provider = "${p.id||'provider-id'}"`,`model_reasoning_effort = "medium"`];if(p.auth_mode==='apikey'){lines.push('',`[model_providers.${p.id||'provider-id'}]`,`name = "${p.name||'供应商名称'}"`,`base_url = "${p.base_url||'https://api.example.com'}"`,`wire_api = "${p.wire_api}"`,`experimental_bearer_token = "***"`)}else lines.push('','# 官方登录模式：使用已捕获的 auth.json 快照');if(p.models.length)lines.push('',`model_catalog_json = "model-catalogs/control-panel-${p.id||'provider-id'}.json"`);$('#config-preview').value=lines.join('\n');$('#auth-preview').value=p.auth_mode==='chatgpt'?'{\n  "auth_mode": "chatgpt",\n  "tokens": "已隐藏"\n}':'{\n  "auth_mode": "apikey",\n  "OPENAI_API_KEY": "***"\n}'}
async function saveProvider(){try{const wasNew=!state.current?.id;const p=gather();if(!p.id||!p.name||!p.model||(p.auth_mode==='apikey'&&!p.base_url)){throw Error('请完整填写名称、模型、Base URL 与供应商标识。')}await api('/api/providers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});state.current=p;note('供应商配置已保存。');await refreshAll();if(wasNew)closeDetail()}catch(e){note(e.message)}}
async function activateCurrent(){try{await saveProvider();const p=gather();if(!p.id)return;const d=await api(`/api/providers/${encodeURIComponent(p.id)}/activate`,{method:'POST'});state.active=p.id;note(`已设为当前供应商，备份编号：${d.backup_id}`);await refreshAll()}catch(e){note(e.message)}}
async function testCurrent(){try{const p=gather();const d=await api('/api/providers/diagnose',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});const failed=(d.checks||[]).filter(item=>item.status==='fail').map(item=>`${item.name}：${item.detail}`);const warnings=(d.checks||[]).filter(item=>item.status==='warning').map(item=>`${item.name}：${item.detail}`);note(d.ok?`诊断通过。${warnings.length?' '+warnings.join('；'):''}`:`诊断发现问题：${failed.join('；')}`)}catch(e){note(e.message)}}
async function loadCommon(){try{const d=await api('/api/common-config');$('#common-config').value=d.contents}catch{}}
async function extractCommon(){try{const d=await api('/api/common-config/extract',{method:'POST'});$('#common-config').value=d.contents;note('已提取当前通用配置。')}catch(e){note(e.message)}}
async function saveCommon(){try{const d=await api('/api/common-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({contents:$('#common-config').value})});$('#common-config').value=d.contents;note('通用配置已保存。')}catch(e){note(e.message)}}
async function saveRoute(){try{const model=$('#route-model').value.trim(),provider_id=$('#route-provider').value;if(!model||!provider_id)throw Error('请选择供应商并输入模型名称。');await api('/api/routes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model,provider_id})});$('#route-model').value='';note('模型快捷路由已保存。');refreshRoutes()}catch(e){note(e.message)}}
async function refreshRoutes(){try{const r=await api('/api/routes');if(r.length)note('已加载 '+r.length+' 条模型快捷路由。','list-notice')}catch{}}
async function previewMigration(){try{const d=await api('/api/sessions/migrate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_provider:$('#migration-target').value,source_provider:$('#migration-source').value||null,apply:false})});$('#migration-result').textContent=`将迁移 ${d.matching_sessions} 个会话标签。`}catch(e){note(e.message)}}
async function applyMigration(){if(!confirm('确认迁移会话供应商标签？系统会创建备份。'))return;try{const d=await api('/api/sessions/migrate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_provider:$('#migration-target').value,source_provider:$('#migration-source').value||null,apply:true})});$('#migration-result').textContent=`已迁移 ${d.changed_sessions||0} 个会话，备份：${d.backup_id||'无'}`;}catch(e){note(e.message)}}
async function toggleSwitch(){try{const enabled=!state.enabled;await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider_switching_enabled:enabled})});state.enabled=enabled;$('#switch').classList.toggle('on',enabled);note(enabled?'已启用供应商切换写入。':'已关闭供应商切换写入；页面仍可编辑档案。','list-notice')}catch(e){note(e.message,'list-notice')}}
function restartHint(){note('供应商切换会直接写入配置；运行中的 Codex 进程需由你在服务器终端重启。','list-notice')}
refreshAll();
</script></body></html>'''

HTML = r'''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codex Provider Console</title>
<style>
body{margin:0;background:#f2f4ef;color:#18231d;font:15px system-ui,sans-serif}.wrap{max-width:920px;margin:48px auto;padding:0 20px}h1{font-size:30px;margin:0 0 6px}.sub{color:#607067;margin:0 0 28px}.grid{display:grid;grid-template-columns:1.2fr .8fr;gap:18px}.panel{background:#fff;border:1px solid #d9e0d8;border-radius:10px;padding:20px;box-shadow:0 8px 25px #17351a0d}h2{font-size:17px;margin:0 0 16px}label{display:block;font-weight:650;margin:11px 0 5px}input,select{width:100%;box-sizing:border-box;padding:9px 10px;border:1px solid #b9c8bc;border-radius:6px;font:inherit}button{border:0;border-radius:6px;padding:9px 13px;background:#146c3d;color:#fff;font-weight:700;cursor:pointer}button.secondary{background:#e6eee8;color:#1d3526}button.danger{background:#a73333}.row{display:flex;gap:8px;align-items:center}.row>*{flex:1}.provider{border-top:1px solid #e2e8e3;padding:13px 0}.provider:first-of-type{border-top:0}.tag{display:inline-block;background:#dceee1;color:#176638;padding:2px 7px;border-radius:999px;font-size:12px;font-weight:700}.muted{color:#69776e;font-size:13px}.notice{margin-top:18px;padding:12px;background:#fff7dd;border:1px solid #eed690;border-radius:7px;color:#644e12}.hidden{display:none}</style>
<body><main class="wrap"><h1>Codex Provider Console</h1><p class="sub">云端供应商配置与切换。密钥仅写入服务器，不会再次显示。</p>
<div class="grid"><section class="panel"><h2>供应商</h2><div id="current" class="muted">正在读取配置…</div><div id="preflight" class="notice hidden"></div><div id="providers"></div></section>
<section class="panel"><h2>添加或更新</h2><form id="form"><label>标识</label><input name="id" required pattern="[A-Za-z0-9_-]{1,48}" placeholder="例如 provider-a"><label>名称</label><input name="name" required placeholder="显示名称"><label>接口地址</label><input name="base_url" required type="url" placeholder="https://api.example.com"><label>默认模型</label><input name="model" value="gpt-5.6-terra" required><label>接入模式</label><select name="auth_mode"><option value="apikey">纯 API</option><option value="chatgpt">官方登录</option></select><label>接口</label><select name="wire_api"><option value="responses">Responses</option><option value="chat">Chat</option></select><label>API Key（官方登录档案无需填写）</label><input name="bearer_token" type="password" placeholder="不会回显"><label>模型列表（每行一个模型名）</label><textarea name="model_lines" rows="4" placeholder="gpt-5.6-terra&#10;gpt-5.6-luna"></textarea><p><button>保存供应商</button> <button type="button" class="secondary" onclick="captureAuth()">捕获当前官方登录</button></p></form><div id="message" class="notice hidden"></div></section></div>
<section class="panel" style="margin-top:18px"><h2>模型快捷路由</h2><p class="muted">选择模型时切换到指定供应商。此功能修改当前 Codex 配置，并非请求级别的聚合代理。</p><div id="routes"></div><form id="route-form"><div class="row"><input name="model" required placeholder="模型名，例如 gpt-5.6-terra"><select name="provider_id" id="route-provider" required></select><button>保存路由</button></div></form></section>
<section class="panel" style="margin-top:18px"><h2>历史会话标识迁移</h2><p class="muted">仅更新会话元数据中的供应商标签，不会删除消息或改变历史内容。操作前会备份每个受影响会话。</p><div class="row"><select id="migration-target"></select><input id="migration-source" placeholder="来源供应商（可留空）"><button type="button" class="secondary" onclick="previewMigration()">预览</button><button type="button" onclick="applyMigration()">执行迁移</button></div><div id="migration-result" class="muted"></div></section>
<section class="panel" style="margin-top:18px"><h2>通用配置文件</h2><p class="muted">保存 MCP、Skills、Plugins 等不属于供应商档案的配置。切换供应商时会与供应商配置合并。</p><p><button type="button" class="secondary" onclick="extractCommon()">从当前 Codex 配置提取</button> <button type="button" onclick="saveCommon()">保存通用配置</button></p><textarea id="common-config" rows="10" placeholder="尚未提取通用配置"></textarea></section>
<div class="notice">当前服务仅绑定到服务器本机地址。接入域名反向代理前，请先配置 HTTPS 与访问认证。</div></main>
<script>
const q=s=>document.querySelector(s);const message=t=>{const e=q('#message');e.textContent=t;e.classList.remove('hidden');setTimeout(()=>e.classList.add('hidden'),3500)};
async function api(url,opt){const r=await fetch(url,opt);const d=await r.json();if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:(d.detail?.message||'请求失败'));return d}
async function refresh(){const d=await api('/api/status');q('#current').innerHTML=`当前供应商：<b>${d.active_provider||'默认 OpenAI'}</b><br>当前模型：<b>${d.model||'未设置'}</b>`;const p=q('#preflight');const warnings=d.preflight.warnings;if(warnings.length){p.innerHTML=`配置提示：${warnings.join(' ')}`;p.classList.remove('hidden')}else{p.classList.add('hidden')}q('#providers').innerHTML=d.profiles.length?d.profiles.map(p=>`<div class="provider"><b>${p.name}</b> <span class="muted">${p.id}</span>${d.active_provider===p.id?' <span class="tag">使用中</span>':''}<div class="muted">${p.base_url} · ${p.model}${p.has_bearer_token?' · 已配置令牌':''}</div><p class="row"><button onclick="activate('${p.id}')">切换</button><button class="secondary" onclick="testProvider('${p.id}')">测试</button><button class="secondary" onclick="edit(${JSON.stringify(p).replace(/"/g,'&quot;')})">编辑</button><button class="danger" onclick="removeProvider('${p.id}')">删除</button></p></div>`).join(''):'<p class="muted">尚未保存供应商档案。</p>'}
async function activate(id){if(!confirm(`切换到 ${id}？系统会先测试连接并备份当前配置。`))return;try{const d=await api(`/api/providers/${id}/activate`,{method:'POST'});message(`已切换，备份编号：${d.backup_id}`);refresh()}catch(err){message(err.message)}}
async function testProvider(id){try{const d=await api(`/api/providers/${id}/test`,{method:'POST'});message(d.ok?`连接正常（HTTP ${d.status}）`:(d.detail||'连接失败'))}catch(err){message(err.message)}}
function edit(p){for(const [k,v]of Object.entries(p)){const e=q(`[name="${k}"]`);if(e&&e.type!=='checkbox'&&k!=='models')e.value=v}q('[name="model_lines"]').value=(p.models||[]).map(m=>m.name).join('\n');window.scrollTo({top:document.body.scrollHeight,behavior:'smooth'})}
async function removeProvider(id){if(!confirm(`删除档案 ${id}？`))return;await api(`/api/providers/${id}`,{method:'DELETE'});refresh()}
q('#form').onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);const data=Object.fromEntries(f);data.models=(data.model_lines||'').split('\n').map(x=>x.trim()).filter(Boolean).map(name=>({name,context_window:'',image_mode:'send-as-is'}));delete data.model_lines;try{await api('/api/providers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});message('供应商档案已保存。');e.target.reset();q('[name="model"]').value='gpt-5.6-terra';refresh();refreshRoutes()}catch(err){message(err.message)}};refresh();
async function captureAuth(){const id=q('[name="id"]').value;if(!id){message('请先填写并保存官方登录档案的标识。');return}try{await api(`/api/providers/${encodeURIComponent(id)}/capture-auth`,{method:'POST'});message('已捕获当前 ChatGPT 登录认证，密钥不会显示。')}catch(err){message(err.message)}}
async function loadCommon(){const d=await api('/api/common-config');q('#common-config').value=d.contents}
async function extractCommon(){try{const d=await api('/api/common-config/extract',{method:'POST'});q('#common-config').value=d.contents;message('已提取当前通用配置。')}catch(err){message(err.message)}}
async function saveCommon(){try{const d=await api('/api/common-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({contents:q('#common-config').value})});q('#common-config').value=d.contents;message('通用配置已保存。')}catch(err){message(err.message)}}
loadCommon();
async function refreshRoutes(){const [routes,status]=await Promise.all([api('/api/routes'),api('/api/status')]);const options=status.profiles.map(p=>`<option value="${p.id}">${p.name} (${p.id})</option>`).join('');q('#route-provider').innerHTML=options;q('#migration-target').innerHTML=options;q('#routes').innerHTML=routes.length?routes.map(r=>`<div class="provider"><b>${r.model}</b><span class="muted"> → ${r.provider_id}</span><p class="row"><button onclick="activateRoute('${r.model}')">使用此路由</button><button class="danger" onclick="removeRoute('${r.model}')">删除</button></p></div>`).join(''):'<p class="muted">尚未创建模型路由。</p>'}
async function activateRoute(model){if(!confirm(`使用 ${model} 的供应商路由？`))return;try{const d=await api(`/api/routes/${encodeURIComponent(model)}/activate`,{method:'POST'});message(`已切换至 ${d.active_provider}，模型为 ${d.model}`);refresh()}catch(err){message(err.message)}}
async function removeRoute(model){if(!confirm(`删除 ${model} 的路由？`))return;await api(`/api/routes/${encodeURIComponent(model)}`,{method:'DELETE'});refreshRoutes()}
q('#route-form').onsubmit=async e=>{e.preventDefault();const data=Object.fromEntries(new FormData(e.target));try{await api('/api/routes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});message('模型路由已保存。');e.target.reset();refreshRoutes()}catch(err){message(err.message)}};
async function previewMigration(){try{const d=await api('/api/sessions/migrate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_provider:q('#migration-target').value,source_provider:q('#migration-source').value||null,apply:false})});q('#migration-result').textContent=`将迁移 ${d.matching_sessions} 个会话标签。`}catch(err){message(err.message)}}
async function applyMigration(){if(!confirm('确认迁移会话供应商标签？系统会创建逐文件备份。'))return;try{const d=await api('/api/sessions/migrate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_provider:q('#migration-target').value,source_provider:q('#migration-source').value||null,apply:true})});q('#migration-result').textContent=`已迁移 ${d.changed_sessions||0} 个会话，备份编号：${d.backup_id||'无'}`;}catch(err){message(err.message)}}
refreshRoutes();
</script>'''
