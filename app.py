import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

CODEX_HOME = Path(os.environ.get("CODEX_HOME", "/codex"))
CODEX_CLI_VERSION = os.environ.get("CODEX_CLI_VERSION", "not_installed")
CODEX_CLI_USER = os.environ.get("CODEX_CLI_USER", "unknown")
DEPLOY_USER = os.environ.get("DEPLOY_USER", "unknown")
USER_HOME = Path(os.environ.get("USER_HOME_PATH", "/user-home"))
HOST_CODEX_BIN = Path(os.environ.get("HOST_CODEX_BIN", "/user-home/.local/bin/codex"))
HOST_CODEX_BIN_CANDIDATES = (
    HOST_CODEX_BIN,
    USER_HOME / ".codex" / "bin" / "codex",
    USER_HOME / ".codex" / "packages" / "standalone" / "current" / "bin" / "codex",
)
HOST_USER_HOME_PATH = os.environ.get("HOST_USER_HOME_PATH", "")
DEPLOYMENT_KEY_PATH = USER_HOME / ".ssh" / "codex-provider-console_ed25519"
DEPLOYMENT_KEY_PUBLIC_PATH = DEPLOYMENT_KEY_PATH.with_suffix(".pub")
AUTHORIZED_KEYS_PATH = USER_HOME / ".ssh" / "authorized_keys"
CONFIG_PATH = CODEX_HOME / "config.toml"
PROFILE_PATH = CODEX_HOME / "control-panel-profiles.json"
SETTINGS_PATH = CODEX_HOME / "control-panel-settings.json"
AUTH_PATH = CODEX_HOME / "auth.json"
BACKUP_ROOT = CODEX_HOME / "backups" / "control-panel"
AUDIT_PATH = CODEX_HOME / "control-panel-audit.jsonl"
PROFILE_ID = re.compile(r"^[a-zA-Z0-9_-]{1,48}$")

app = FastAPI(title="Codex Provider Console", docs_url=None, redoc_url=None)

AUTH_ENABLED = os.environ.get("PANEL_AUTH_ENABLED", "true").lower() not in {"0", "false", "no"}
PANEL_USERNAME = os.environ.get("PANEL_USERNAME", "")
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "")
SESSION_SECRET = os.environ.get("PANEL_SESSION_SECRET", "")
COOKIE_SECURE = os.environ.get("PANEL_COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}
SESSION_COOKIE = "codex_panel_session"
SESSION_MAX_AGE = 12 * 60 * 60
LOGIN_TIMEOUT_SECONDS = 10


class DeviceLoginSession:
    """Owns one local Codex app-server device-code login without exposing tokens."""

    def __init__(self, provider_id: str):
        self.provider_id = provider_id
        self.process: subprocess.Popen[str] | None = None
        self.lock = threading.Lock()
        self.next_id = 1
        self.waiters: dict[int, tuple[threading.Event, dict]] = {}
        self.login_id = ""
        self.verification_url = ""
        self.user_code = ""
        self.status = "starting"
        self.detail = "正在启动官方登录服务…"
        self.plan_type = ""
        self.captured = False

    def _reader(self) -> None:
        assert self.process and self.process.stdout
        for raw in self.process.stdout:
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            response_id = message.get("id")
            if isinstance(response_id, int):
                with self.lock:
                    waiter = self.waiters.get(response_id)
                    if waiter:
                        waiter[1]["message"] = message
                        waiter[0].set()
                continue
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            if message.get("method") == "account/login/completed" and params.get("loginId") == self.login_id:
                with self.lock:
                    if params.get("success"):
                        self.status = "completed"
                        self.detail = "登录已完成，正在保存认证状态。"
                    else:
                        self.status = "failed"
                        self.detail = str(params.get("error") or "官方登录未完成")[:300]
            elif message.get("method") == "account/updated":
                with self.lock:
                    self.plan_type = str(params.get("planType") or "")

    def _send(self, payload: dict) -> None:
        if not self.process or not self.process.stdin:
            raise RuntimeError("Codex login service is unavailable")
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict) -> dict:
        with self.lock:
            request_id = self.next_id
            self.next_id += 1
            event = threading.Event()
            holder: dict = {}
            self.waiters[request_id] = (event, holder)
            self._send({"method": method, "id": request_id, "params": params})
        if not event.wait(LOGIN_TIMEOUT_SECONDS):
            with self.lock:
                self.waiters.pop(request_id, None)
            raise RuntimeError("Codex login service timed out")
        message = holder.get("message", {})
        if message.get("error"):
            raise RuntimeError(str(message["error"].get("message") or "Codex login service rejected the request"))
        result = message.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Codex login service returned an invalid response")
        return result

    def start(self) -> dict:
        env = os.environ.copy()
        env["CODEX_HOME"] = str(CODEX_HOME)
        try:
            self.process = subprocess.Popen(
                ["codex", "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=env,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("Codex CLI is unavailable in the control panel container. Update the deployment and try again.") from exc
        threading.Thread(target=self._reader, daemon=True).start()
        self.request("initialize", {"clientInfo": {"name": "codex_provider_console", "title": "Codex Provider Console", "version": "1.0"}})
        self._send({"method": "initialized", "params": {}})
        result = self.request("account/login/start", {"type": "chatgptDeviceCode"})
        self.login_id = str(result.get("loginId") or "")
        self.verification_url = str(result.get("verificationUrl") or "")
        self.user_code = str(result.get("userCode") or "")
        if not self.login_id or not self.verification_url or not self.user_code:
            raise RuntimeError("Codex did not return a device login code")
        self.status = "pending"
        self.detail = "请在浏览器完成官方登录。"
        return self.public_status()

    def cancel(self) -> None:
        if self.status == "pending" and self.login_id:
            try:
                self.request("account/login/cancel", {"loginId": self.login_id})
            except RuntimeError:
                pass
        self.status = "cancelled"
        self.detail = "登录已取消。"
        if self.process and self.process.poll() is None:
            self.process.terminate()

    def public_status(self) -> dict:
        if self.process and self.process.poll() is not None and self.status in {"starting", "pending"}:
            self.status = "failed"
            self.detail = "Codex 登录服务已退出。"
        return {
            "provider_id": self.provider_id,
            "status": self.status,
            "detail": self.detail,
            "verification_url": self.verification_url if self.status == "pending" else "",
            "user_code": self.user_code if self.status == "pending" else "",
            "plan_type": self.plan_type,
            "captured": self.captured,
        }


DEVICE_LOGIN_LOCK = threading.Lock()
DEVICE_LOGIN: DeviceLoginSession | None = None


class ModelEntry(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class Provider(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,48}$")
    name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(default="", pattern=r"^(|https?://.+)")
    wire_api: str = Field(default="responses", pattern=r"^(responses|chat)$")
    model: str = Field(default="", max_length=120)
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


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


def auth_configured() -> bool:
    return bool(PANEL_USERNAME and PANEL_PASSWORD and SESSION_SECRET)


def session_token(username: str) -> str:
    payload = f"{int(time.time())}:{username}".encode("utf-8")
    signature = hmac.new(SESSION_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest().encode("ascii")
    return base64.urlsafe_b64encode(payload + b":" + signature).decode("ascii").rstrip("=")


def valid_session(token: str | None) -> bool:
    if not token or not auth_configured():
        return False
    try:
        padded = token + "=" * (-len(token) % 4)
        payload, signature = base64.urlsafe_b64decode(padded.encode("ascii")).rsplit(b":", 1)
        expected = hmac.new(SESSION_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest().encode("ascii")
        timestamp, username = payload.decode("utf-8").split(":", 1)
        return (
            hmac.compare_digest(signature, expected)
            and hmac.compare_digest(username, PANEL_USERNAME)
            and int(timestamp) + SESSION_MAX_AGE >= int(time.time())
        )
    except (ValueError, UnicodeDecodeError):
        return False


LOGIN_HTML = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>登录 · Codex 控制台</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f4f6f8;color:#202124;font:14px Arial,"Microsoft YaHei",sans-serif}.login{width:min(380px,calc(100vw - 36px));background:#fff;border:1px solid #dde1e6;border-radius:8px;padding:28px;box-sizing:border-box;box-shadow:0 12px 32px #0000000d}h1{margin:0;font-size:22px}p{color:#687078;line-height:1.5}label{display:block;font-weight:700;margin:17px 0 7px}input{box-sizing:border-box;width:100%;border:1px solid #cfd5da;border-radius:6px;padding:10px 11px;font:inherit}button{width:100%;border:0;border-radius:6px;background:#202124;color:#fff;padding:11px;margin-top:20px;font:inherit;font-weight:700;cursor:pointer}#message{min-height:20px;color:#bd3131;margin-top:12px}</style></head><body><main class="login"><h1>Codex 控制台</h1><p>请输入管理员账号和密码。</p><form id="login"><label>用户名<input id="username" autocomplete="username" required autofocus></label><label>密码<input id="password" type="password" autocomplete="current-password" required></label><button>登录</button><div id="message"></div></form></main><script>document.querySelector('#login').addEventListener('submit',async e=>{e.preventDefault();const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:username.value,password:password.value})});if(r.ok)location.href='/';else{const d=await r.json().catch(()=>({}));message.textContent=d.detail||'登录失败。'}})</script></body></html>'''


@app.middleware("http")
async def authentication(request: Request, call_next):
    if not AUTH_ENABLED or request.url.path in {"/login", "/logout"}:
        return await call_next(request)
    if not auth_configured():
        detail = "Panel authentication is enabled but not configured. Run bash install.sh --force."
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": detail}, status_code=503)
        return HTMLResponse(detail, status_code=503)
    if valid_session(request.cookies.get(SESSION_COOKIE)):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Authentication required"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page() -> str:
    return LOGIN_HTML


@app.post("/login")
def login(request: LoginRequest) -> JSONResponse:
    if not AUTH_ENABLED:
        return JSONResponse({"authenticated": True})
    if not auth_configured():
        raise HTTPException(503, "Panel authentication is not configured")
    if not (hmac.compare_digest(request.username, PANEL_USERNAME) and hmac.compare_digest(request.password, PANEL_PASSWORD)):
        raise HTTPException(401, "用户名或密码错误")
    response = JSONResponse({"authenticated": True})
    response.set_cookie(SESSION_COOKIE, session_token(PANEL_USERNAME), max_age=SESSION_MAX_AGE, httponly=True, samesite="strict", secure=COOKIE_SECURE, path="/")
    return response


@app.post("/logout")
def logout() -> JSONResponse:
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


def read_profiles() -> dict[str, dict]:
    if not PROFILE_PATH.exists():
        return {}
    try:
        return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(500, "Provider profile store is invalid") from exc


def panel_settings() -> dict:
    if not SETTINGS_PATH.exists():
        return {"provider_switching_enabled": True, "reverse_proxy": {}}
    try:
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(500, "Control panel settings store is invalid") from exc
    return {
        "provider_switching_enabled": bool(settings.get("provider_switching_enabled", True)),
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


def mounted_executable_path(cli_path: Path) -> Path | None:
    """Resolve a host CLI path through the deployment user's mounted home."""
    current_path = cli_path
    seen_paths: set[Path] = set()

    # The official installer often creates a chain such as:
    # ~/.local/bin/codex -> ~/.codex/.../codex.
    # Container mounts cannot follow absolute host-home links automatically.
    for _ in range(8):
        if current_path in seen_paths:
            return None
        seen_paths.add(current_path)

        if not current_path.is_symlink():
            if current_path.is_file() and os.access(current_path, os.X_OK):
                return current_path
            return None

        try:
            target = Path(os.readlink(current_path))
        except OSError:
            return None

        if not target.is_absolute():
            current_path = current_path.parent / target
            continue
        if not HOST_USER_HOME_PATH:
            return None
        try:
            relative_target = target.relative_to(HOST_USER_HOME_PATH)
        except ValueError:
            return None
        current_path = USER_HOME / relative_target
    return None


def executable_user_cli_path() -> Path | None:
    for cli_path in HOST_CODEX_BIN_CANDIDATES:
        executable_path = mounted_executable_path(cli_path)
        if executable_path:
            return executable_path
    return None


def health_check() -> dict:
    checks: list[dict[str, str]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    add("Codex 数据目录", "pass" if CODEX_HOME.exists() else "fail", str(CODEX_HOME) if CODEX_HOME.exists() else f"目录不存在：{CODEX_HOME}")
    # The panel mounts the deployment user's home. This is the only filesystem
    # view shared reliably by rootful and rootless Docker deployments.
    cli_path = executable_user_cli_path()
    cli_version = ""
    if cli_path:
        try:
            cli_version = subprocess.run([str(cli_path), "--version"], capture_output=True, text=True, timeout=8, check=False).stdout.strip().splitlines()[0]
        except (OSError, subprocess.SubprocessError, IndexError):
            cli_version = ""
    # The version recorded at deployment time can be stale. Run the resolved
    # user CLI instead; install.sh maintains /usr/local/bin/codex as its stable
    # non-interactive SSH entry point.
    cli_installed = bool(cli_version)
    if cli_installed:
        cli_detail = f"{cli_version}；已验证部署用户 {DEPLOY_USER} 的实际 CLI。"
    else:
        cli_detail = f"用户 {DEPLOY_USER} 未检测到 Codex CLI；可在此页直接安装。"
    add(
        "Codex CLI",
        "pass" if cli_installed else "fail",
        cli_detail,
    )
    deployment_key_ready, deployment_key_detail = deployment_key_status()
    add("Codex Desktop 部署密钥", "pass" if deployment_key_ready else "warning", deployment_key_detail)
    add("目录写入权限", "pass" if CODEX_HOME.exists() and os.access(CODEX_HOME, os.W_OK) else "fail", "可写" if CODEX_HOME.exists() and os.access(CODEX_HOME, os.W_OK) else "控制台无法写入 Codex 数据目录")

    config = config_text()
    if CONFIG_PATH.exists():
        add("config.toml", "pass" if config.strip() else "warning", "已找到配置文件" if config.strip() else "配置文件为空")
        provider_match = re.search(r"^\s*model_provider\s*=\s*['\"]([^'\"]+)['\"]", config, re.M)
        model_match = re.search(r"^\s*model\s*=\s*['\"]([^'\"]+)['\"]", config, re.M)
        if provider_match:
            detail = f"供应商：{provider_match.group(1)}"
            if model_match:
                detail += f"，默认模型：{model_match.group(1)}"
            else:
                detail += "；未设置默认模型（可在 Codex 中自行选择）"
            add("Codex 关键配置", "pass", detail)
        else:
            add("Codex 关键配置", "warning", "缺少 model_provider；保存并激活供应商后会自动补齐")
    else:
        add("config.toml", "warning", "尚未生成；保存并激活供应商后会创建")

    if AUTH_PATH.exists():
        try:
            parsed_auth = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
            add("auth.json", "pass" if isinstance(parsed_auth, dict) else "fail", "JSON 格式有效" if isinstance(parsed_auth, dict) else "必须是 JSON 对象")
        except (OSError, json.JSONDecodeError):
            add("auth.json", "fail", "文件不可读或不是有效 JSON")
    else:
        add("auth.json", "warning", "未找到；官方登录和纯 API 供应商都会使用该文件保存认证，保存并激活供应商后会自动创建")

    if not auth_configured() and AUTH_ENABLED:
        add("控制台认证", "fail", "认证已启用但管理员账号、密码或会话密钥未配置")
    else:
        add("控制台认证", "pass", "认证配置完整" if AUTH_ENABLED else "认证已关闭")

    profiles = read_profiles()
    active_id = active_provider()
    active = profiles.get(active_id) if active_id else None
    if not active:
        add("当前供应商", "warning", "尚未激活供应商；请先保存并激活一个供应商")
    else:
        add("当前供应商", "pass", f"{active.get('name', active_id)} ({active_id})")
        diagnostic = diagnose_profile(active)
        for item in diagnostic.get("checks", []):
            add(f"供应商 · {item['name']}", item["status"], item["detail"])

    try:
        usage = shutil.disk_usage(CODEX_HOME)
        free_gb = usage.free / (1024 ** 3)
        add("磁盘空间", "pass" if free_gb >= 1 else "warning", f"剩余 {free_gb:.1f} GB")
    except OSError as exc:
        add("磁盘空间", "warning", f"无法读取磁盘空间：{exc}")

    failed = sum(item["status"] == "fail" for item in checks)
    warnings = sum(item["status"] == "warning" for item in checks)
    audit("health_checked", failed=failed, warnings=warnings)
    return {
        "ok": failed == 0,
        "passed": failed == 0 and warnings == 0,
        "checks": checks,
        "summary": "环境满足使用条件。" if failed == 0 and warnings == 0 else f"发现 {failed} 项失败、{warnings} 项提醒。",
    }


def deployment_key_status() -> tuple[bool, str]:
    if not DEPLOYMENT_KEY_PATH.is_file() or not DEPLOYMENT_KEY_PUBLIC_PATH.is_file():
        return False, "尚未生成面板管理的部署密钥；可在此页创建、部署并下载。"
    try:
        public_key = DEPLOYMENT_KEY_PUBLIC_PATH.read_text(encoding="utf-8").strip()
        authorized_keys = AUTHORIZED_KEYS_PATH.read_text(encoding="utf-8") if AUTHORIZED_KEYS_PATH.exists() else ""
    except OSError:
        return False, "部署密钥文件不可读；请检查部署用户主目录权限。"
    if public_key and public_key in authorized_keys:
        return True, f"已部署到用户 {DEPLOY_USER} 的 authorized_keys；可下载私钥供 Codex Desktop 使用。"
    return False, "部署密钥尚未写入 authorized_keys；可在此页重新部署。"


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
    selected_model = str(model_override or profile.get("model") or "").strip()
    generated = [
        f'model_provider = {toml_quote(provider_id)}',
        f'[model_providers.{provider_id}]',
        f'name = {toml_quote(profile["name"])}',
        f'requires_openai_auth = {str(auth_mode == "chatgpt").lower()}',
    ]
    if selected_model:
        generated.insert(1, f'model = {toml_quote(selected_model)}')
    if auth_mode == "apikey":
        generated.insert(5, f'wire_api = {toml_quote(profile["wire_api"])}')
    if auth_mode == "apikey" and profile.get("base_url"):
        generated.insert(5, f'base_url = {toml_quote(profile["base_url"].rstrip("/"))}')
    if profile.get("bearer_token"):
        generated.append(f'experimental_bearer_token = {toml_quote(profile["bearer_token"])}')
    if catalog_path:
        generated.insert(2 if selected_model else 1, f'model_catalog_json = {toml_quote(catalog_path)}')
    provider_config = profile.get("config_contents", "").strip()
    if provider_config:
        masked_token = 'experimental_bearer_token = "***"'
        if profile.get("bearer_token"):
            provider_config = provider_config.replace(masked_token, f'experimental_bearer_token = {toml_quote(profile["bearer_token"])}')
        # The current UI may save feature-only settings such as [features].
        # Those settings supplement a provider; they must not replace the
        # model_provider entry that identifies the active provider.
        if not re.search(r"^\s*model_provider\s*=", provider_config, re.M):
            provider_config = "\n".join(generated) + "\n\n" + provider_config
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
    audit("provider_switched", provider_id=provider_id, backup_id=backup_id, model=selected_model or None)
    return {"active_provider": provider_id, "backup_id": backup_id, "check": check, "model": selected_model or None, "auth_mode": auth_mode}


@app.get("/api/status")
def status() -> dict:
    model = None
    if CONFIG_PATH.exists():
        match = re.search(r'^\s*model\s*=\s*"([^"]+)"', CONFIG_PATH.read_text(encoding="utf-8"), re.M)
        model = match.group(1) if match else None
    return {"active_provider": active_provider(), "model": model, "profiles": [public_profile(p) for p in read_profiles().values()], "preflight": preflight(), "settings": panel_settings()}


class PanelSettings(BaseModel):
    provider_switching_enabled: bool = True
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


@app.get("/api/health")
def get_health() -> dict:
    return health_check()


@app.post("/api/health/install-codex")
def install_codex_from_health() -> dict:
    if not HOST_CODEX_BIN.parent.parent.exists():
        raise HTTPException(409, "部署未挂载当前用户的主目录；请更新面板后重试")
    install_env = os.environ.copy()
    # CODEX_HOME is the panel's /codex data mount. Passing it to the official
    # installer makes ~/.local/bin/codex point at a container-only path.
    install_env.pop("CODEX_HOME", None)
    install_env.update(
        {
            "HOME": str(USER_HOME),
            "PATH": f"{USER_HOME}/.local/bin:{USER_HOME}/.codex/bin:{USER_HOME}/.codex/packages/standalone/current/bin:/usr/local/bin:/usr/bin:/bin",
        }
    )
    launcher = USER_HOME / ".local" / "bin" / "codex"
    if launcher.is_symlink():
        try:
            if os.readlink(launcher).startswith("/codex/"):
                launcher.unlink()
        except OSError:
            pass
    try:
        result = subprocess.run(
            ["/bin/sh", "-c", "curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=true sh"],
            env=install_env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "Codex CLI 安装超时") from exc
    cli_path = executable_user_cli_path()
    if result.returncode != 0 or not cli_path:
        detail = (result.stderr or result.stdout or "官方安装器未完成").strip()[-500:]
        raise HTTPException(502, f"Codex CLI 安装失败：{detail}")
    version = subprocess.run([str(cli_path), "--version"], capture_output=True, text=True, timeout=8, check=False).stdout.strip().splitlines()[0]
    audit("codex_cli_installed_from_health", user=DEPLOY_USER, version=version)
    return {"version": version, "detail": f"Codex CLI 已安装到用户 {DEPLOY_USER}；健康检查已刷新。"}


@app.post("/api/health/deployment-key")
def deploy_ssh_key() -> dict:
    ssh_dir = DEPLOYMENT_KEY_PATH.parent
    if not USER_HOME.is_dir() or not os.access(USER_HOME, os.W_OK):
        raise HTTPException(409, "部署未挂载当前用户的主目录或目录不可写；请更新面板后重试")
    if not shutil.which("ssh-keygen"):
        raise HTTPException(409, "面板镜像未包含 ssh-keygen；请更新面板后重试")
    ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ssh_dir, 0o700)
    if DEPLOYMENT_KEY_PATH.exists() and not DEPLOYMENT_KEY_PUBLIC_PATH.exists():
        raise HTTPException(409, "发现不完整的部署私钥；为避免覆盖现有密钥，请先人工处理后再试")
    if DEPLOYMENT_KEY_PUBLIC_PATH.exists() and not DEPLOYMENT_KEY_PATH.exists():
        raise HTTPException(409, "发现没有对应私钥的部署公钥；为避免覆盖现有文件，请先人工处理后再试")
    if not DEPLOYMENT_KEY_PATH.exists():
        result = subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-f", str(DEPLOYMENT_KEY_PATH), "-N", "", "-C", f"codex-provider-console-{DEPLOY_USER}"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "ssh-keygen 未完成").strip()[-300:]
            raise HTTPException(502, f"生成部署密钥失败：{detail}")
    try:
        public_key = DEPLOYMENT_KEY_PUBLIC_PATH.read_text(encoding="utf-8").strip()
        if not public_key:
            raise ValueError("公钥为空")
        existing = AUTHORIZED_KEYS_PATH.read_text(encoding="utf-8") if AUTHORIZED_KEYS_PATH.exists() else ""
        if public_key not in existing:
            with AUTHORIZED_KEYS_PATH.open("a", encoding="utf-8") as handle:
                if existing and not existing.endswith("\n"):
                    handle.write("\n")
                handle.write(public_key + "\n")
        os.chmod(DEPLOYMENT_KEY_PATH, 0o600)
        os.chmod(DEPLOYMENT_KEY_PUBLIC_PATH, 0o644)
        os.chmod(AUTHORIZED_KEYS_PATH, 0o600)
    except (OSError, ValueError) as exc:
        raise HTTPException(502, f"部署公钥失败：{exc}") from exc
    audit("deployment_ssh_key_created", user=DEPLOY_USER)
    return {"detail": "部署密钥已生成并授权。现在请下载私钥，并在 Codex App 中选择该文件。", "download_url": "/api/health/deployment-key/download"}


@app.get("/api/health/deployment-key/download")
def download_ssh_key() -> FileResponse:
    ready, _ = deployment_key_status()
    if not ready:
        raise HTTPException(404, "部署密钥尚未完成创建和授权")
    audit("deployment_ssh_key_downloaded", user=DEPLOY_USER)
    return FileResponse(
        DEPLOYMENT_KEY_PATH,
        media_type="application/octet-stream",
        filename=f"codex-provider-console-{DEPLOY_USER}.ed25519",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/providers/{provider_id}/capture-auth")
def capture_chatgpt_auth(provider_id: str) -> dict:
    capture_chatgpt_snapshot(provider_id)
    return {"provider_id": provider_id, "captured": True}


def capture_chatgpt_snapshot(provider_id: str) -> None:
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


@app.post("/api/providers/{provider_id}/official-login/start")
def start_official_login(provider_id: str) -> dict:
    global DEVICE_LOGIN
    profile = read_profiles().get(provider_id)
    if not profile:
        raise HTTPException(404, "Provider profile not found")
    if profile.get("auth_mode") != "chatgpt":
        raise HTTPException(422, "Only a ChatGPT login provider can start official login")
    with DEVICE_LOGIN_LOCK:
        if DEVICE_LOGIN and DEVICE_LOGIN.status == "pending":
            DEVICE_LOGIN.cancel()
        DEVICE_LOGIN = DeviceLoginSession(provider_id)
        try:
            result = DEVICE_LOGIN.start()
        except RuntimeError as exc:
            DEVICE_LOGIN.status = "failed"
            DEVICE_LOGIN.detail = str(exc)[:300]
            raise HTTPException(503, DEVICE_LOGIN.detail) from exc
    audit("chatgpt_device_login_started", provider_id=provider_id)
    return result


@app.get("/api/official-login/status")
def official_login_status() -> dict:
    with DEVICE_LOGIN_LOCK:
        if not DEVICE_LOGIN:
            return {"status": "idle", "detail": "尚未开始官方登录。", "captured": False}
        status = DEVICE_LOGIN.public_status()
        if DEVICE_LOGIN.status == "completed" and not DEVICE_LOGIN.captured:
            try:
                capture_chatgpt_snapshot(DEVICE_LOGIN.provider_id)
            except (HTTPException, OSError) as exc:
                DEVICE_LOGIN.status = "failed"
                DEVICE_LOGIN.detail = str(getattr(exc, "detail", exc))[:300]
            else:
                DEVICE_LOGIN.captured = True
                DEVICE_LOGIN.detail = "官方登录已更新并安全保存。"
                audit("chatgpt_device_login_completed", provider_id=DEVICE_LOGIN.provider_id)
            status = DEVICE_LOGIN.public_status()
        return status


@app.post("/api/official-login/cancel")
def cancel_official_login() -> dict:
    with DEVICE_LOGIN_LOCK:
        if not DEVICE_LOGIN:
            return {"status": "idle", "detail": "没有正在进行的官方登录。"}
        DEVICE_LOGIN.cancel()
        audit("chatgpt_device_login_cancelled", provider_id=DEVICE_LOGIN.provider_id)
        return DEVICE_LOGIN.public_status()


@app.post("/api/runtime/restart")
def restart_managed_codex_runtime() -> dict:
    """Reset only the panel-owned device-login session, never a user shell."""
    global DEVICE_LOGIN
    with DEVICE_LOGIN_LOCK:
        if DEVICE_LOGIN:
            DEVICE_LOGIN.cancel()
            DEVICE_LOGIN = None
    audit("managed_codex_runtime_reset")
    return {
        "restarted": True,
        "detail": "供应商配置已应用。控制台没有可安全重启的常驻 Codex 进程；之后从控制台发起的 Codex 会话会读取新配置，SSH 终端中手动运行的 Codex 不会被中断。",
    }


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
    official_panel = '''<div id="official-fields" class="field-help" style="display:none"><div>官方登录使用设备码流程，不写入或显示 API Key。</div><div style="margin-top:8px"><button id="official-login-btn" type="button" class="btn light small" onclick="startOfficialLogin()">开始官方登录</button> <button id="official-refresh-btn" type="button" class="btn light small" onclick="startOfficialLogin()">刷新令牌（重新登录）</button> <button id="official-cancel-btn" type="button" class="btn light small" onclick="cancelOfficialLogin()" style="display:none">取消登录</button></div><div id="official-login-flow" style="display:none;margin-top:10px;padding:10px 12px;border:1px solid #d2d6da;border-radius:7px;background:#f8fafc"><div id="official-login-status">正在准备登录…</div><div id="official-login-code" style="display:none;margin-top:8px;font-weight:700;font-family:monospace"></div><a id="official-login-link" target="_blank" rel="noopener" style="display:none;margin-top:6px;word-break:break-all"></a></div><div id="capture-auth-status" class="muted" style="margin-top:7px">请先保存该档案，再开始官方登录。</div><div style="margin-top:7px">登录在你的浏览器完成；令牌只写入服务器的 Codex 目录，不会在页面显示。</div></div>'''
    old_panel = '''<div id="official-fields" class="field-help" style="display:none">官方登录档案会使用当前服务器的 Codex 登录状态。保存档案后点击“捕获当前官方登录”建立加密前的本地认证快照；快照不会在页面显示。</div>'''
    official_script = r'''<script>
document.head.insertAdjacentHTML('beforeend','<style>#model-list.model-list-box{min-height:150px;max-height:300px;overflow:auto;border:1px solid #d2d6da;border-radius:7px;background:#fff;padding:10px 12px}.model-entry{line-height:1.8;font:14px "Consolas","Microsoft YaHei",sans-serif;color:#2c3035}.model-entry:empty{display:none}.doctor-mask{display:none;position:fixed;inset:0;background:#0008;z-index:1000;align-items:center;justify-content:center}.doctor-mask.show{display:flex}.doctor-card{width:min(560px,calc(100vw - 32px));background:#fff;border-radius:11px;padding:19px;box-shadow:0 18px 60px #0004}.doctor-title{font-size:18px;font-weight:500}.doctor-summary{margin:8px 0 12px;color:#656b73}.doctor-progress{height:8px;background:#e8ebee;border-radius:999px;overflow:hidden;margin-bottom:10px}.doctor-progress i{display:block;height:100%;width:0;background:#1683ff;border-radius:inherit;transition:width .22s}#doctor-state{padding:3px 8px;background:#f3f4f5;border-radius:999px;font-size:12px;color:#30343a}.doctor-check{min-height:64px;border:1px solid #e1e4e8;border-radius:9px;padding:12px 12px 12px 50px;margin-top:10px;position:relative}.doctor-check:before{content:"✓";position:absolute;left:15px;top:17px;width:20px;height:20px;border-radius:50%;background:#f0f1f2;color:#1683ff;display:grid;place-items:center;font-size:12px;font-weight:700}.doctor-check b{display:block}.doctor-check small{display:block;color:#6a7078;margin-top:5px;line-height:1.45}.doctor-check.real-result.expanded{height:130px;overflow:hidden}.doctor-check.real-result.expanded small{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:5;overflow:hidden}.doctor-check.running{border-color:#acd4ff}.doctor-check.running:before{content:"◌"}.doctor-check.fail:before{content:"!";color:#ed5b5b}.doctor-check.warning:before{content:"";background:#f0f1f2}.doctor-advice{margin-top:10px}.doctor-advice .doctor-check{margin-top:0}.doctor-advice .doctor-check:before{content:"◆";color:#e49a13;font-size:10px}</style>');
document.head.insertAdjacentHTML('beforeend','<style>.btn:disabled{background:#c8ccd1;color:#777d85;cursor:not-allowed}#panel-dialog-mask{display:none;position:fixed;inset:0;z-index:1200;background:#11182773;align-items:center;justify-content:center;padding:20px}#panel-dialog-mask.show{display:flex}.panel-dialog{width:min(460px,100%);box-sizing:border-box;border:1px solid #dfe3e7;border-radius:8px;background:#fff;padding:20px;box-shadow:0 22px 60px #0003}.panel-dialog h3{margin:0;color:#202328;font-size:18px}.panel-dialog p{margin:10px 0 0;color:#4d5560;line-height:1.6}.panel-dialog-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:20px}.panel-dialog-actions .btn{min-width:76px}</style>');
document.head.insertAdjacentHTML('beforeend','<style>#list-view.console-content{max-width:none;margin:22px 0 22px 228px;padding:0 26px}.provider-actions{display:flex;gap:6px;margin-left:auto;opacity:0;pointer-events:none;transition:opacity .15s}.provider-card{cursor:default}.provider-card:hover .provider-actions,.provider-card:focus-within .provider-actions{opacity:1;pointer-events:auto}.provider-action{min-width:52px;height:30px;padding:0 9px;border:1px solid #d7d9dd;border-radius:6px;background:#fff;color:#30343a;font:inherit;font-size:12px;cursor:pointer}.provider-action:hover{border-color:#8a929c;background:#f4f5f6}.provider-action.danger{color:#b42318}.provider-main{min-width:0}.provider-card.active .provider-action{background:#fff}@media(max-width:800px){#list-view.console-content{margin-left:190px}}@media(max-width:760px){#list-view.console-content{padding:0 13px}.provider-card{align-items:flex-start;flex-wrap:wrap}.provider-actions{width:100%;margin-left:47px;opacity:1;pointer-events:auto}.provider-action{flex:1}}</style>');
document.head.insertAdjacentHTML('beforeend','<style>.provider-current{display:inline-flex;align-items:center;justify-content:center;box-sizing:border-box;background:#eef1f4;color:#5e6770;cursor:default}</style>');
document.body.insertAdjacentHTML('beforeend','<div id="doctor-mask" class="doctor-mask"><div class="doctor-card"><div class="row-between"><div class="doctor-title">Provider Doctor</div><span id="doctor-state" class="section-hint"></span><button class="back" onclick="closeDoctor()">×</button></div><div id="doctor-summary" class="doctor-summary"></div><div class="doctor-progress"><i id="doctor-progress"></i></div><div id="doctor-checks"></div><div id="doctor-advice" class="doctor-advice"></div><p style="margin:16px 0 0"><button id="doctor-close" class="btn light" onclick="closeDoctor()">关闭</button></p></div></div>');
document.body.insertAdjacentHTML('beforeend','<div id="panel-dialog-mask" role="dialog" aria-modal="true" aria-labelledby="panel-dialog-title"><div class="panel-dialog"><h3 id="panel-dialog-title"></h3><p id="panel-dialog-message"></p><div class="panel-dialog-actions"><button id="panel-dialog-cancel" class="btn light" type="button">取消</button><button id="panel-dialog-confirm" class="btn" type="button">确认</button></div></div></div>');
const modelList=$('#model-list');const modelHead=modelList?.previousElementSibling,modelHelp=modelHead?.previousElementSibling,modelTitle=modelHelp?.previousElementSibling;if(modelHead){modelHead.remove()}if(modelTitle){modelTitle.querySelector('button')?.remove()}if(modelList){modelList.classList.add('model-list-box');modelList.setAttribute('aria-readonly','true')}if(modelHelp)modelHelp.textContent='模型名称仅能通过“从上游获取”填入。';
function setModelListVisibility(official){const list=$('#model-list');if(!list)return;const modelHelp=list.previousElementSibling,modelTitle=modelHelp?.previousElementSibling;[list,modelHelp,modelTitle].forEach(node=>{if(node)node.style.display=official?'none':''})}
let officialLoginPoll=null;
function renderOfficialLogin(status){const flow=$('#official-login-flow'),label=$('#official-login-status'),code=$('#official-login-code'),link=$('#official-login-link'),cancel=$('#official-cancel-btn'),start=$('#official-login-btn'),refresh=$('#official-refresh-btn');if(!flow)return;const pending=status.status==='pending';flow.style.display=status.status==='idle'?'none':'';label.textContent=status.detail||'';code.style.display=pending?'block':'none';code.textContent=pending?`一次性验证码：${status.user_code}`:'';link.style.display=pending?'block':'none';if(pending){link.href=status.verification_url;link.textContent=status.verification_url}cancel.style.display=pending?'':'none';start.disabled=!state.current?.id||pending;refresh.disabled=!state.current?.id||pending;const saved=status.status==='completed'&&status.captured;if(saved){state.current={...state.current,has_auth_snapshot:true};$('#capture-auth-status').textContent='官方登录已更新并安全保存。';note('官方登录已完成，认证令牌不会显示。');refreshAll()}else if(status.status==='failed'||status.status==='cancelled'){$('#capture-auth-status').textContent=status.detail||'官方登录未完成。'}}
async function pollOfficialLogin(){try{const status=await api('/api/official-login/status');renderOfficialLogin(status);if(status.status!=='pending'&&officialLoginPoll){clearInterval(officialLoginPoll);officialLoginPoll=null}}catch(e){if(officialLoginPoll){clearInterval(officialLoginPoll);officialLoginPoll=null}note(e.message)}}
function authModeChanged(){const official=$('#p-auth').value==='chatgpt';$('#api-fields').style.display=official?'none':'grid';$('#official-fields').style.display=official?'block':'none';$('#p-url').required=!official;$('#test-btn').style.display=official?'none':'';const protocols=$('.protocols'),protocolTitle=protocols?.previousElementSibling;if(protocols)protocols.style.display=official?'none':'flex';if(protocolTitle)protocolTitle.style.display=official?'none':'';setModelListVisibility(official);if(official){const captured=state.current?.has_auth_snapshot;$('#capture-auth-status').textContent=captured?'已保存官方登录；可用“刷新令牌”重新登录。':'请先保存该档案，再开始官方登录。';$('#official-login-btn').disabled=!state.current?.id;$('#official-refresh-btn').disabled=!state.current?.id;pollOfficialLogin()}updatePreview()}
async function startOfficialLogin(){try{if($('#p-auth').value!=='chatgpt')throw Error('仅官方登录档案可开始登录。');const saved=gather();if(!saved.id)throw Error('请先填写供应商名称。');await saveProvider();if(!state.current||state.current.id!==saved.id)openProvider(saved.id);const status=await api(`/api/providers/${encodeURIComponent(saved.id)}/official-login/start`,{method:'POST'});renderOfficialLogin(status);if(officialLoginPoll)clearInterval(officialLoginPoll);officialLoginPoll=setInterval(pollOfficialLogin,1500)}catch(e){note(e.message)}}
async function cancelOfficialLogin(){try{const status=await api('/api/official-login/cancel',{method:'POST'});renderOfficialLogin(status);if(officialLoginPoll){clearInterval(officialLoginPoll);officialLoginPoll=null}}catch(e){note(e.message)}}
async function fetchModelsFromUpstream(){try{if($('#p-auth').value!=='apikey')throw Error('从上游获取仅适用于纯 API 供应商。');const base_url=$('#p-url').value.trim(),bearer_token=$('#p-key').value;if(!base_url||!bearer_token)throw Error('请先填写 Base URL 和 Key。');const button=$('#fetch-models-btn');button.disabled=true;button.textContent='正在获取…';const result=await api('/api/upstream/models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({base_url,bearer_token})});$('#model-list').innerHTML='';result.models.forEach(name=>addModel({name}));if(!state.testModel&&result.models.length)state.testModel=result.models[0];note(`已从上游获取 ${result.models.length} 个模型。`);updatePreview()}catch(e){note(e.message)}finally{const button=$('#fetch-models-btn');if(button){button.disabled=false;button.textContent='⇩ 从上游获取'}}}
function installFetchModelsButton(){const list=$('#model-list');if(!list||$('#fetch-models-btn'))return;const modelHelp=list.previousElementSibling,modelTitle=modelHelp?.previousElementSibling;if(!modelTitle)return;const button=document.createElement('button');button.id='fetch-models-btn';button.type='button';button.className='btn light small';button.textContent='⇩ 从上游获取';button.onclick=fetchModelsFromUpstream;modelTitle.append(button)}
installFetchModelsButton()
function newProvider(){state.current={id:'',name:'',base_url:'',model:'',wire_api:'responses',auth_mode:'chatgpt',models:[],goals_enabled:false,goals_configured:false,test_model:''};openDetail()}
function setManagedRestartAvailable(available){const button=$('#managed-restart-btn');if(!button)return;button.disabled=!available;button.title=available?'重启面板托管的 Codex 服务并读取当前配置':'请先成功切换供应商'}
async function restartManagedCodex(){const button=$('#managed-restart-btn');if(!button||button.disabled)return;try{button.disabled=true;button.textContent='正在重启…';const result=await api('/api/runtime/restart',{method:'POST'});note(result.detail,'list-notice');}catch(e){note(e.message,'list-notice');button.disabled=false;}finally{button.textContent='重启 Codex';button.title='请先成功切换供应商'}}
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
function panelDialog({title,message,confirmLabel='确认',cancelLabel='取消',showCancel=true}){const mask=$('#panel-dialog-mask'),titleNode=$('#panel-dialog-title'),messageNode=$('#panel-dialog-message'),confirmButton=$('#panel-dialog-confirm'),cancelButton=$('#panel-dialog-cancel');titleNode.textContent=title;messageNode.textContent=message;confirmButton.textContent=confirmLabel;cancelButton.textContent=cancelLabel;cancelButton.style.display=showCancel?'':'none';mask.classList.add('show');return new Promise(resolve=>{const close=value=>{mask.classList.remove('show');confirmButton.onclick=null;cancelButton.onclick=null;resolve(value)};confirmButton.onclick=()=>close(true);cancelButton.onclick=()=>close(false)})}
function doctorStatus(items){return items.some(item=>item.status==='fail')?'fail':items.some(item=>item.status==='warning')?'warning':'pass'}
function showDoctorProgress(){const stages=[['配置完整性','正在检查配置完整性…'],['模型列表','等待检查 /v1/models…'],['真实请求','等待发送一次测试请求…'],['处理建议','等待生成处理建议。']];let step=0;$('#doctor-state').textContent='诊断中';$('#doctor-summary').textContent='正在诊断供应商，请稍候。';$('#doctor-progress').style.width='18%';$('#doctor-checks').innerHTML=stages.map(([name,detail],index)=>`<div class="doctor-check ${index===0?'running':''}"><b>${name}</b><small>${detail}</small></div>`).join('');$('#doctor-advice').textContent='诊断中';$('#doctor-close').style.display='none';$('#doctor-mask').classList.add('show');return setInterval(()=>{step=Math.min(step+1,3);$('#doctor-progress').style.width=`${18+step*19}%`;[...document.querySelectorAll('#doctor-checks .doctor-check')].forEach((node,index)=>node.classList.toggle('running',index===step))},360)}
function showDoctor(result){const all=result.checks||[];const configuration=all.filter(item=>['Base URL','API Key','上游协议','官方认证'].includes(item.name));const models=all.filter(item=>item.name.includes('模型目录')||item.name==='配置模型可见性');const real=all.filter(item=>item.name==='真实请求');const groups=[['配置完整性',configuration],['模型列表',models],['真实请求',real]];$('#doctor-state').textContent=result.passed?'完成':result.ok?'待确认':'异常';$('#doctor-summary').textContent=result.summary;$('#doctor-progress').style.width='100%';$('#doctor-checks').innerHTML=groups.map(([name,items])=>{const status=doctorStatus(items),detail=items.length?items.map(item=>item.detail).join('；'):'该步骤未执行。';const expanded=name==='真实请求'&&items.some(item=>item.status==='pass');return `<div class="doctor-check ${status} ${expanded?'real-result expanded':''}"><b>${esc(name)}</b><small>${esc(detail)}</small></div>`}).join('');const failed=all.filter(item=>item.status==='fail'),modelWarning=all.find(item=>item.name==='配置模型可见性'&&item.status==='warning'),modelFailure=all.find(item=>item.name==='模型目录'||item.name==='上游模型目录'),realFailure=all.find(item=>item.name==='真实请求'&&item.status==='fail');const advice=result.passed?'可以作为 Codex 供应商使用；如果真实对话仍失败，请查看协议代理日志里的上游响应。':failed.some(item=>item.name==='Base URL'||item.name==='API Key')?'先补齐 Base URL 和 API Key；如果使用官方账号，请切换到官方登录模式。':modelWarning?'连接可用，但测试模型没有出现在模型列表里；建议改用上游返回的模型名。':modelFailure?.status==='fail'?'优先检查 Base URL 是否包含正确的 /v1 前缀，以及供应商是否支持 /v1/models。':realFailure?'优先检查测试模型名称、上游协议选择和 Key 权限；如果 Chat Completions 可用，请切到对应协议。':'请检查上游服务配置。';$('#doctor-advice').innerHTML=`<div class="doctor-check"><b>处理建议</b><small>${esc(advice)}</small></div>`;$('#doctor-close').style.display='';$('#doctor-mask').classList.add('show')}
async function testCurrent(){const timer=showDoctorProgress();try{const [d]=await Promise.all([api('/api/providers/diagnose',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(gather())}),new Promise(resolve=>setTimeout(resolve,450))]);clearInterval(timer);showDoctor(d)}catch(e){clearInterval(timer);$('#doctor-state').textContent='异常';$('#doctor-summary').textContent='诊断请求失败。';$('#doctor-progress').style.width='100%';$('#doctor-checks').innerHTML=`<div class="doctor-check fail"><b>诊断错误</b><small>${esc(e.message)}</small></div>`;$('#doctor-advice').textContent='请检查控制台服务、网络连接和上游配置。';$('#doctor-close').style.display='';}}
 </script>'''
    navigation_script = r'''<script>
 document.head.insertAdjacentHTML('beforeend', `<style>
 .console-sidebar{position:fixed;inset:0 auto 0 0;width:228px;background:#202124;color:#f7f8fa;padding:22px 14px;z-index:20;display:flex;flex-direction:column;gap:22px}.console-brand{font-size:17px;font-weight:750;padding:0 12px}.console-brand small{display:block;color:#aeb4bb;font-size:11px;font-weight:400;margin-top:5px}.console-nav{display:grid;gap:5px}.console-nav button{border:0;background:transparent;color:#cfd3d8;text-align:left;border-radius:7px;padding:11px 12px;font:inherit;cursor:pointer}.console-nav button:hover,.console-nav button.active{background:#34373b;color:#fff}.console-logout{margin-top:auto;border:0;background:transparent;color:#cfd3d8;text-align:left;border-radius:7px;padding:11px 12px;font:inherit;cursor:pointer}.console-logout:hover{background:#34373b;color:#fff}.console-content{margin-left:228px}.console-panel{max-width:1320px;margin:22px auto;padding:0 26px}.console-panel .list-shell{background:#fff;border:1px solid #dde1e6;border-radius:11px;padding:18px}.console-panel h2{margin:0 0 7px;font-size:18px}.console-panel .panel-note{color:#686e76;font-size:13px;margin:0 0 18px}.console-form{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.console-form label{display:block;color:#555c64;font-size:12px;margin-bottom:5px}.console-form input,.console-form select,.console-form textarea{width:100%;box-sizing:border-box;padding:9px 10px;border:1px solid #d2d6da;border-radius:7px;font:inherit;background:#fff}.console-form textarea{min-height:104px;resize:vertical}.console-form .wide{grid-column:1/-1}.console-form-actions{display:flex;gap:8px;margin-top:17px}.console-code{font:12px Consolas,monospace;background:#f4f5f6;color:#30343a;padding:12px;border-radius:7px;white-space:pre-wrap;overflow:auto}.console-muted{color:#727982;font-size:12px}.console-health-summary{margin:8px 0 14px;padding:11px 13px;background:#f4f5f6;border-radius:7px;color:#4b525a}.health-check{border:1px solid #dfe3e7;border-left:4px solid #2f9e63;border-radius:7px;padding:10px 12px;margin-top:8px}.health-check.warning{border-left-color:#d18b16;background:#fffaf0}.health-check.fail{border-left-color:#d64545;background:#fff5f5}.health-check b,.health-check small{display:block}.health-check small{margin-top:4px;color:#686e76;line-height:1.45}
 @media(max-width:800px){.console-sidebar{width:190px}.console-content{margin-left:190px}.console-form{grid-template-columns:1fr}}
 </style>`);
 document.body.insertAdjacentHTML('afterbegin', `<aside class="console-sidebar"><div class="console-brand">Codex 控制台<small>通用服务器管理</small></div><nav class="console-nav"><button data-section="providers" onclick="openConsoleSection('providers')">供应商配置</button><button data-section="health" onclick="openConsoleSection('health')">健康检查</button><button data-section="proxy" onclick="openConsoleSection('proxy')">反向代理</button></nav><button class="console-logout" onclick="logoutConsole()">退出登录</button></aside>`);
 document.querySelector('.top')?.classList.add('console-content');document.querySelectorAll('.page,.detail-top,.detail-main').forEach(e=>e.classList.add('console-content'));
 document.body.insertAdjacentHTML('beforeend', `<section id="console-health" class="console-panel console-nav-panel" style="display:none"><div class="list-shell"><div class="row-between"><div><h2>健康检查</h2><p class="panel-note">检查 Codex 数据目录、配置文件、认证、当前供应商和磁盘空间，确认服务器是否满足使用条件。</p></div><button class="btn" onclick="runHealth()">立即检查</button></div><div id="health-summary" class="console-health-summary">尚未执行检查。</div><div id="health-checks"></div></div></section><section id="console-proxy" class="console-panel console-nav-panel" style="display:none"><div class="list-shell"><h2>反向代理</h2><p class="panel-note">为域名访问生成 Nginx 配置片段。建议启用 HTTPS 和访问认证后再公开服务。</p><div class="console-form"><div><label>域名</label><input id="proxy-domain" placeholder="console.example.com"></div><div><label>上游地址</label><input id="proxy-upstream" value="127.0.0.1:8787"></div><div><label>TLS 证书路径</label><input id="proxy-cert" placeholder="/etc/letsencrypt/live/example/fullchain.pem"></div><div><label>TLS 私钥路径</label><input id="proxy-key" placeholder="/etc/letsencrypt/live/example/privkey.pem"></div><div class="wide"><label>Nginx 配置预览</label><pre id="proxy-config" class="console-code">填写域名后生成</pre></div></div><div class="console-form-actions"><button class="btn" onclick="saveConsoleSettings()">保存反向代理配置</button></div><div id="proxy-notice" class="notice"></div></div></section>`);
 requestAnimationFrame(()=>document.body.classList.add('console-ready'));
 async function logoutConsole(){await fetch('/logout',{method:'POST'});location.href='/login'}
 function openConsoleSection(section){document.querySelectorAll('.console-nav button').forEach(b=>b.classList.toggle('active',b.dataset.section===section));document.querySelectorAll('.console-nav-panel').forEach(p=>p.style.display='none');const list=document.querySelector('#list-view'),detail=document.querySelector('#detail');if(section==='providers'){if(list)list.style.display='';if(detail&&detail.classList.contains('visible'))detail.style.display='';}else{if(list)list.style.display='none';if(detail)detail.style.display='none';document.querySelector('#console-'+section).style.display='block';if(section==='health')runHealth()}localStorage.setItem('console-section',section)}
 async function runHealth(){const summary=$('#health-summary'),list=$('#health-checks');summary.textContent='正在检查服务器环境和供应商连通性…';list.innerHTML='';try{const d=await api('/api/health');summary.textContent=d.summary;list.innerHTML=(d.checks||[]).map(item=>{const cliAction=item.name==='Codex CLI'&&item.status==='fail'?'<p><button class="btn small" onclick="installCodexCli(this)">安装 Codex CLI</button></p>':'';const keyAction=item.name==='Codex Desktop 部署密钥'?`<p><button class="btn small" onclick="${item.status==='pass'?'downloadDeploymentKey()':'deployDeploymentKey(this)'}">${item.status==='pass'?'下载部署密钥':'创建并部署密钥'}</button></p>`:'';return `<div class="health-check ${item.status}"><b>${item.status==='pass'?'通过':item.status==='warning'?'提醒':'失败'} · ${esc(item.name)}</b><small>${esc(item.detail)}</small>${cliAction}${keyAction}</div>`}).join('')}catch(e){summary.textContent='健康检查失败：'+e.message}}
 async function installCodexCli(button){const confirmed=await panelDialog({title:'安装 Codex CLI',message:'将为部署用户安装官方 Codex CLI。安装完成后会自动重新检查环境。',confirmLabel:'开始安装'});if(!confirmed)return;button.disabled=true;button.textContent='正在安装…';try{const result=await api('/api/health/install-codex',{method:'POST'});await panelDialog({title:'Codex CLI 已安装',message:result.detail,confirmLabel:'完成',showCancel:false});await runHealth()}catch(e){await panelDialog({title:'安装失败',message:e.message,confirmLabel:'知道了',showCancel:false});button.disabled=false;button.textContent='安装 Codex CLI'}}
 async function deployDeploymentKey(button){const confirmed=await panelDialog({title:'创建部署密钥',message:'将为当前部署用户生成新的 SSH 私钥，并将对应公钥加入 authorized_keys。私钥不会在页面显示，但可由已登录的面板重复下载；请勿分享给他人。',confirmLabel:'创建并部署'});if(!confirmed)return;button.disabled=true;button.textContent='正在部署…';try{const result=await api('/api/health/deployment-key',{method:'POST'});const download=await panelDialog({title:'部署密钥已创建',message:`${result.detail}。私钥可在已登录面板中重复下载，请安全保存且不要分享。`,confirmLabel:'立即下载',cancelLabel:'稍后下载'});if(download)window.location.href=result.download_url;await runHealth()}catch(e){await panelDialog({title:'部署失败',message:e.message,confirmLabel:'知道了',showCancel:false});button.disabled=false;button.textContent='创建并部署密钥'}}
 function downloadDeploymentKey(){window.location.href='/api/health/deployment-key/download'}
 function updateProxyConfig(){const domain=$('#proxy-domain')?.value.trim(),upstream=$('#proxy-upstream')?.value.trim()||'127.0.0.1:8787',cert=$('#proxy-cert')?.value.trim(),key=$('#proxy-key')?.value.trim();$('#proxy-config').textContent=domain?`server {\n    listen 443 ssl;\n    server_name ${domain};\n    ssl_certificate ${cert||'/path/to/fullchain.pem'};\n    ssl_certificate_key ${key||'/path/to/privkey.pem'};\n    location / {\n        proxy_pass http://${upstream};\n        proxy_set_header Host $host;\n        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n    }\n}`:'填写域名后生成'}
 ['proxy-domain','proxy-upstream','proxy-cert','proxy-key'].forEach(id=>document.getElementById(id)?.addEventListener('input',updateProxyConfig));
 async function loadConsoleSettings(){try{const s=await api('/api/settings'),proxy=s.reverse_proxy||{};for(const [id,key] of [['proxy-domain','domain'],['proxy-upstream','upstream'],['proxy-cert','cert'],['proxy-key','key']])if(proxy[key]!=null)$('#'+id).value=proxy[key];updateProxyConfig()}catch{}}
 async function saveConsoleSettings(){try{const s=await api('/api/settings'),payload={provider_switching_enabled:s.provider_switching_enabled,reverse_proxy:{domain:$('#proxy-domain').value.trim(),upstream:$('#proxy-upstream').value.trim()||'127.0.0.1:8787',cert:$('#proxy-cert').value.trim(),key:$('#proxy-key').value.trim()}};await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});note('反向代理配置已保存。','proxy-notice')}catch(e){note(e.message)}}
 loadConsoleSettings();openConsoleSection(localStorage.getItem('console-section')||'providers');
 </script>'''
    return (
        NEW_HTML.replace(old_panel, official_panel)
        .replace('<button class="btn outline" onclick="loadCommon()">通用配置</button>', "")
        .replace('<button class="btn" onclick="restartHint()">重启 Codex</button>', '<button id="managed-restart-btn" class="btn" onclick="restartManagedCodex()" disabled title="请先成功切换供应商">应用配置</button>')
        .replace('<button class="btn light" onclick="loadCommon()">提取通用配置</button>', "")
        .replace('<button class="btn light" onclick="newProvider(\'apikey\')">＋ 添加供应商</button><button class="btn light" onclick="newProvider(\'chatgpt\')">＋ 添加官方登录供应商</button>', '<button class="btn light" onclick="newProvider()">＋ 添加供应商</button>')
        .replace('每行一个模型；上下文窗口和图片处理方式将一并保存到供应商档案。', '每行一个模型名称；可手动输入，或从上游获取后自动填入。')
        .replace('<div class="model-head"><span>模型名称</span><span>上下文窗口</span><span>图片处理方式</span><span></span></div>', '<div class="model-head"><span>模型名称</span><span></span></div>')
        .replace('<div class="preview-note">切换到此供应商时会写入的预览；不会显示密钥。</div><textarea id="config-preview" class="config-area" readonly>', '<div class="preview-note">可手动补充或修改；切换到此供应商时会写入，密钥以掩码保存。</div><textarea id="config-preview" class="config-area" oninput="state.configTouched=true">')
        .replace('<textarea id="auth-preview" class="config-area" readonly>', '<textarea id="auth-preview" class="config-area">')
        .replace('<input id="p-key" type="password" placeholder="保存后不再显示">', '<input id="p-key" type="text" placeholder="API Key">')
        .replace('placeholder="例如 fhl"', 'placeholder="例如 chatgpt"')
        .replace('<input id="p-model" value="gpt-5.6-terra" placeholder="例如 gpt-5.6-terra" oninput="updatePreview()">', '<input id="p-model" placeholder="例如 gpt-5.6-terra" oninput="updatePreview()">')
        .replace('<div class="field"><label>名称</label><input id="p-name" placeholder="例如 chatgpt" oninput="updatePreview()"></div>', '<div class="field"><label>名称</label><input id="p-name" placeholder="例如 chatgpt_1" oninput="updatePreview()"><div class="field-help">系统会据此自动生成供应商标识，用于写入 Codex 配置；请使用英文、数字、`-` 或 `_`。</div></div>')
        .replace('<div class="field"><label>配置模型</label><input id="p-model" placeholder="例如 gpt-5.6-terra" oninput="updatePreview()"><div class="field-help">默认启动 Codex 时使用的模型名称。</div></div>', '<div class="field"><label>配置模型（可选）</label><input id="p-model" placeholder="例如 gpt-5.6-terra" oninput="updatePreview()"><div class="field-help">留空时不写入默认模型，可在 Codex 中自行选择。</div></div>')
        .replace('<div class="field"><label>Codex 目标</label><select id="p-target"><option value="">不启用目标功能</option></select></div>', '<div class="field"><label>Codex 目标</label><label style="display:flex;align-items:center;gap:8px;border:1px solid #d2d6da;border-radius:7px;padding:10px 12px;font-weight:400"><input id="p-goals" type="checkbox" onchange="syncGoalsConfig()" style="width:auto">启用目标功能</label></div>')
         .replace("</body></html>", official_script + navigation_script + "</body></html>")
    )


NEW_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>供应商配置 · Codex</title>
<style>
body:not(.console-ready) .top,body:not(.console-ready) .page{visibility:hidden}:root{--line:#e5e7eb;--text:#24272b;--muted:#686e76;--bg:#f7f8fa;--blue:#1683ff;--blue-fill:#e8f3ff;--black:#202124}
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
function renderList(){const profiles=state.profiles;$('#list-count').textContent=`${profiles.length} 个供应商配置；点击编辑按钮进入详情`;$(' #provider-list'.trim()).innerHTML=profiles.length?profiles.map(p=>{const active=p.id===state.active;return `<div class="provider-card ${active?'active':''}"><div class="handle">⋮⋮</div><div class="badge">${esc((p.name||p.id).slice(0,1).toUpperCase())}</div><div class="provider-main"><div class="card-name">${esc(p.name)} ${active?'<span class="section-hint">使用中</span>':''}</div><div class="card-meta">${p.auth_mode==='chatgpt'?'官方登录':'纯 API'} · ${p.wire_api==='responses'?'Responses API':'Chat Completions'} · ${esc(p.base_url||'不写入 API 文件')}</div></div><div class="provider-actions">${active?'<span class="provider-action provider-current" aria-label="当前使用的供应商">使用中</span>':`<button class="provider-action" type="button" title="使用此供应商" onclick="activateFromList('${esc(p.id)}')">使用</button>`}<button class="provider-action" type="button" title="编辑供应商" onclick="openProvider('${esc(p.id)}')">编辑</button><button class="provider-action danger" type="button" title="删除供应商" onclick="deleteProvider('${esc(p.id)}')">删除</button></div></div>`}).join(''):'<div class="empty">尚未添加供应商。可先添加 API 供应商或官方登录档案。</div>'}
async function activateFromList(id){try{const result=await api(`/api/providers/${encodeURIComponent(id)}/activate`,{method:'POST'});state.active=id;setManagedRestartAvailable(true);note(`已使用 ${id}，备份编号：${result.backup_id}。现在可应用配置。`,'list-notice');await refreshAll()}catch(e){note(e.message,'list-notice')}}
async function deleteProvider(id){if(!confirm(`确认删除供应商「${id}」？`))return;try{await api(`/api/providers/${encodeURIComponent(id)}`,{method:'DELETE'});note('供应商已删除。','list-notice');await refreshAll()}catch(e){note(e.message,'list-notice')}}
function populateSelectors(){const o=state.profiles.map(p=>`<option value="${esc(p.id)}">${esc(p.name)} (${esc(p.id)})</option>`).join('');const routeProvider=$('#route-provider'),migrationTarget=$('#migration-target');if(routeProvider)routeProvider.innerHTML=o;if(migrationTarget)migrationTarget.innerHTML=o}
function newProvider(mode='apikey'){state.current={id:'',name:'',base_url:'',model:'gpt-5.6-terra',wire_api:'responses',auth_mode:mode,models:[]};openDetail()}
function openProvider(id){const p=state.profiles.find(x=>x.id===id);if(!p)return;state.current=structuredClone(p);openDetail()}
function openDetail(){const p=state.current;$('#list-view').classList.add('hidden');$('#detail').classList.add('visible');$('#detail-name').textContent=p.id? p.name:'添加供应商';$('#detail-sub').textContent=p.id===state.active?'当前正在使用':'编辑后保存列表，再切换模式时会使用新配置';$('#p-name').value=p.name||'';$('#p-model').value=p.model||'gpt-5.6-terra';$('#p-url').value=p.base_url||'';$('#p-key').value='';$('#p-auth').value=p.auth_mode||'apikey';state.protocol=p.wire_api||'responses';setProtocol(state.protocol);$('#model-list').innerHTML='';(p.models?.length?p.models:[{name:p.model||'',context_window:'1M',image_mode:'send-as-is'}]).forEach(addModel);authModeChanged();updatePreview()}
function closeDetail(){$('#detail').classList.remove('visible');$('#list-view').classList.remove('hidden');state.current=null;refreshAll()}
function authModeChanged(){const official=$('#p-auth').value==='chatgpt';$('#api-fields').style.display=official?'none':'grid';$('#official-fields').style.display=official?'block':'none';$('#p-url').required=!official;updatePreview()}
function setProtocol(v){state.protocol=v;$('#responses-tab').classList.toggle('selected',v==='responses');$('#chat-tab').classList.toggle('selected',v==='chat');updatePreview()}
function addModel(m={name:'',context_window:'1M',image_mode:'send-as-is'}){const row=document.createElement('div');row.className='model-row';row.innerHTML=`<input placeholder="例如 gpt-5.6-terra" value="${esc(m.name)}" oninput="updatePreview()"><input placeholder="1M" value="${esc(m.context_window||'')}" oninput="updatePreview()"><select onchange="updatePreview()"><option value="send-as-is">send-as-is</option><option value="omit">omit</option></select><button class="remove-model" title="删除模型" onclick="this.parentElement.remove();updatePreview()">×</button>`;row.querySelector('select').value=m.image_mode||'send-as-is';$('#model-list').append(row)}
function gather(){const id=(state.current?.id||$('#p-name').value.trim().toLowerCase().replace(/[^a-z0-9_-]+/g,'-')).replace(/^-+|-+$/g,'');return {id,name:$('#p-name').value.trim(),base_url:$('#p-url').value.trim(),model:$('#p-model').value.trim(),wire_api:state.protocol,auth_mode:$('#p-auth').value,bearer_token:$('#p-key').value,models:[...document.querySelectorAll('.model-row')].map(r=>({name:r.children[0].value.trim(),context_window:r.children[1].value.trim(),image_mode:r.children[2].value})).filter(m=>m.name)}}
function updatePreview(){if(!state.current)return;const p=gather();const lines=[`model = "${p.model||'gpt-5.6-terra'}"`,`model_provider = "${p.id||'provider-id'}"`,`model_reasoning_effort = "medium"`];if(p.auth_mode==='apikey'){lines.push('',`[model_providers.${p.id||'provider-id'}]`,`name = "${p.name||'供应商名称'}"`,`base_url = "${p.base_url||'https://api.example.com'}"`,`wire_api = "${p.wire_api}"`,`experimental_bearer_token = "***"`)}else lines.push('','# 官方登录模式：使用已捕获的 auth.json 快照');if(p.models.length)lines.push('',`model_catalog_json = "model-catalogs/control-panel-${p.id||'provider-id'}.json"`);$('#config-preview').value=lines.join('\n');$('#auth-preview').value=p.auth_mode==='chatgpt'?'{\n  "auth_mode": "chatgpt",\n  "tokens": "已隐藏"\n}':'{\n  "auth_mode": "apikey",\n  "OPENAI_API_KEY": "***"\n}'}
async function saveProvider(){try{const wasNew=!state.current?.id;const p=gather();if(!p.id||!p.name||(p.auth_mode==='apikey'&&!p.base_url)){throw Error('请填写供应商名称；纯 API 还需要 Base URL。供应商标识会由名称自动生成。')}await api('/api/providers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});state.current=p;note('供应商配置已保存。');await refreshAll();if(wasNew)closeDetail()}catch(e){note(e.message)}}
async function activateCurrent(){try{await saveProvider();const p=gather();if(!p.id)return;const d=await api(`/api/providers/${encodeURIComponent(p.id)}/activate`,{method:'POST'});state.active=p.id;setManagedRestartAvailable(true);note(`已设为当前供应商，备份编号：${d.backup_id}。现在可点击“重启 Codex”让面板托管服务读取新配置。`);await refreshAll()}catch(e){note(e.message)}}
function setManagedRestartAvailable(available){const button=$('#managed-restart-btn');if(!button)return;button.disabled=!available;button.title=available?'确认后续控制台会话读取当前供应商配置':'请先成功切换供应商'}
async function restartManagedCodex(){const button=$('#managed-restart-btn');if(!button||button.disabled)return;try{button.disabled=true;button.textContent='正在应用…';const result=await api('/api/runtime/restart',{method:'POST'});note(result.detail,'list-notice');}catch(e){note(e.message,'list-notice');button.disabled=false;}finally{button.textContent='应用配置';button.title='请先成功切换供应商'}}
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
