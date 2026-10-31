import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import secrets
import socket
import smtplib
import sqlite3
import subprocess
import tomllib
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, Literal

from fastapi import APIRouter, BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from provider_domain import (
    backfill_profile_model,
    is_profile_usable,
    normalize_profile,
    storage_profile,
    remote_session_archive_args,
    remote_session_delete_args,
    remote_session_unarchive_args,
    resolve_host_codex_home,
)
from host_ops import host_ssh_command as build_host_ssh_command, nss_environment, ssh_host_key_options as verify_ssh_host_key
from storage_ops import write_private as atomic_write_private
from api.compat import AuthService, ProviderStore
from application.backup_service import BackupService
from application.provider_service import ProviderService
from application.provider_activation import ProviderActivationValidationError, build_activation_plan
from application.provider_switch_service import ProviderSwitchDependencies, ProviderSwitchService, activation_error_message
from application.health_service import HealthDependencies, HealthService
from application.task_store import TaskStore
from application.session_catalog import SessionCatalog, SessionCatalogPaths
from application.session_service import SessionError, SessionService
from application.recovery_service import RecoveryError, RecoveryService
from application.totp_service import TOTPError, TOTPService
from application.diagnostic_service import DiagnosticService
from application.official_login import DeviceLoginSession
from application.config_service import (
    canonicalize_base_config,
    normalize_features_sections,
    remove_goals_feature,
    remove_provider_sections,
    set_goals_feature,
    split_toml_document,
    toml_quote,
)
CODEX_HOME = Path(os.environ.get("CODEX_HOME", "/codex"))
APP_VERSION = (Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip() if Path(__file__).with_name("VERSION").is_file() else "0.1.0")
CODEX_CLI_VERSION = os.environ.get("CODEX_CLI_VERSION", "not_installed")
CODEX_CLI_USER = os.environ.get("CODEX_CLI_USER", "unknown")
CODEX_LATEST_RELEASE_URL = "https://releases.openai.com/codex/channels/latest"
DEPLOY_USER = os.environ.get("DEPLOY_USER", "unknown")
USER_HOME = Path(os.environ.get("USER_HOME_PATH", "/user-home"))
HOST_CODEX_BIN = Path(os.environ.get("HOST_CODEX_BIN", "/user-home/.local/bin/codex"))
HOST_CODEX_BIN_CANDIDATES = (
    HOST_CODEX_BIN,
    USER_HOME / ".codex" / "bin" / "codex",
    USER_HOME / ".codex" / "packages" / "standalone" / "current" / "bin" / "codex",
)
HOST_USER_HOME_PATH = os.environ.get("HOST_USER_HOME_PATH", "")
HOST_CODEX_HOME_PATH = os.environ.get("HOST_CODEX_HOME_PATH", "")
PANEL_SSH_HOST_ALIAS = os.environ.get("PANEL_SSH_HOST_ALIAS", "").strip()
DEPLOYMENT_KEY_PATH = USER_HOME / ".ssh" / "codex-provider-console_ed25519"
DEPLOYMENT_KEY_PUBLIC_PATH = DEPLOYMENT_KEY_PATH.with_suffix(".pub")
AUTHORIZED_KEYS_PATH = USER_HOME / ".ssh" / "authorized_keys"
CONFIG_PATH = CODEX_HOME / "config.toml"
PROFILE_PATH = CODEX_HOME / "control-panel-profiles.json"
SETTINGS_PATH = CODEX_HOME / "control-panel-settings.json"
AUTH_PATH = CODEX_HOME / "auth.json"
COMMON_CONFIG_PATH = CODEX_HOME / "control-panel-common-config.toml"
RELAY_BASE_URL = os.environ.get("RELAY_BASE_URL", "http://codex-provider-relay:57321/v1")
# The panel reaches the relay over Docker DNS, but the host-side Codex App
# Server cannot resolve that container name. Generated Codex config must use
# the host-published loopback endpoint instead.
HOST_RELAY_BASE_URL = os.environ.get("HOST_RELAY_BASE_URL", "http://127.0.0.1:57321/v1")
BACKUP_ROOT = CODEX_HOME / "backups" / "control-panel"
BACKUP_METADATA_NAME = "metadata.json"
BACKUP_ID = re.compile(r"^[0-9]{8}T[0-9]{6,12}Z$")
# Provider switches are routine operations. Keep a small rollback window for
# them without touching snapshots created by explicit repair or migration
# operations.
PROVIDER_SWITCH_BACKUP_RETENTION = 5
AUDIT_PATH = CODEX_HOME / "control-panel-audit.jsonl"
PROFILE_ID = re.compile(r"^[a-zA-Z0-9_-]{1,48}$")
THREAD_ID = re.compile(r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
THREAD_ID_IN_TEXT = re.compile(r"(?i)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
SESSIONS_PATH = CODEX_HOME / "sessions"
SESSION_INDEX_PATH = CODEX_HOME / "session_index.jsonl"
# Codex stores a model-provider identifier with every conversation.  Keep
# that identifier independent of a panel profile so changing profiles does
# not make old conversations refer to a deleted TOML provider table.
SESSION_PROVIDER_ID = "custom"
ARCHIVED_SESSIONS_PATH = CODEX_HOME / "archived_sessions"
APP_SERVER_LOCK = threading.Lock()
SESSION_MUTATION_LOCK = threading.Lock()
SESSIONS_ROUTER = APIRouter()
BACKUPS_ROUTER = APIRouter()
PROXY_ROUTER = APIRouter()
ACCOUNT_ROUTER = APIRouter()
PROVIDERS_ROUTER = APIRouter()
DIAGNOSTICS_ROUTER = APIRouter()
OPERATIONS_ROUTER = APIRouter()
PROVIDER_SWITCH_LOCK = threading.Lock()
ACTIVATION_JOBS: dict[str, dict] = {}
ACTIVATION_JOBS_LOCK = threading.Lock()
MODEL_DIAGNOSTIC_JOBS: dict[str, dict] = {}
MODEL_DIAGNOSTIC_JOBS_LOCK = threading.Lock()
PROVIDER_DIAGNOSTIC_JOBS: dict[str, dict] = {}
PROVIDER_DIAGNOSTIC_JOBS_LOCK = threading.Lock()
HEALTH_CHECK_JOBS: dict[str, dict] = {}
HEALTH_CHECK_JOBS_LOCK = threading.Lock()
REVERSE_PROXY_LOCK = threading.Lock()
REVERSE_PROXY_JOBS: dict[str, dict] = {}
REVERSE_PROXY_JOBS_LOCK = threading.Lock()

TASK_STORE = TaskStore(CODEX_HOME / "control-panel-jobs.sqlite3")

def new_background_job(kind: str, payload: dict) -> dict:
    return TASK_STORE.new_job(kind, payload)

for _kind, _job in TASK_STORE.recover():
    _registry = {
        "activation": ACTIVATION_JOBS,
        "health": HEALTH_CHECK_JOBS,
        "provider_diagnostic": PROVIDER_DIAGNOSTIC_JOBS,
        "model_diagnostic": MODEL_DIAGNOSTIC_JOBS,
        "reverse_proxy": REVERSE_PROXY_JOBS,
    }.get(_kind)
    if _registry is not None and _job.get("id"):
        _registry[_job["id"]] = TASK_STORE.new_job(_kind, _job)
# The Compose reverse-proxy reaches this service over the internal network.
REVERSE_PROXY_UPSTREAM = "codex-provider-console:8787"
HOST_APP_SERVER_STOP_SCRIPT = r'''
import os
import shlex
import signal
import shutil
import subprocess
import time
from pathlib import Path

codex_bin = Path.home() / ".local" / "bin" / "codex"
if not codex_bin.is_file() or not os.access(codex_bin, os.X_OK):
    codex_bin = Path(shutil.which("codex") or "")
if not codex_bin.is_file():
    raise SystemExit("Codex CLI not found; expected ~/.local/bin/codex or a PATH entry")
codex = str(codex_bin)

managed = subprocess.run(
    [codex, "app-server", "daemon", "stop"],
    text=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
)
own_pid = os.getpid()
stopped = 0
if managed.returncode != 0:
    for line in subprocess.check_output(["ps", "-eo", "pid,args"], text=True, errors="replace").splitlines()[1:]:
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        pid, command = int(parts[0]), parts[1]
        try:
            argv = shlex.split(command)
        except ValueError:
            continue
        is_app_server = len(argv) >= 2 and Path(argv[0]).name == "codex" and "app-server" in argv
        if pid != own_pid and is_app_server:
            try:
                os.kill(pid, signal.SIGTERM)
                stopped += 1
            except ProcessLookupError:
                pass

deadline = time.monotonic() + 10
while time.monotonic() < deadline:
    remaining = 0
    for raw in subprocess.check_output(["ps", "-eo", "args"], text=True, errors="replace").splitlines()[1:]:
        try:
            argv = shlex.split(raw.strip())
        except ValueError:
            continue
        if len(argv) >= 2 and Path(argv[0]).name == "codex" and "app-server" in argv and "proxy" not in argv and ("pid-update-loop" not in argv or managed.returncode != 0):
            remaining += 1
    if not remaining:
        break
    time.sleep(0.1)
else:
    detail = managed.stdout.strip()
    suffix = f": {detail}" if detail else ""
    raise SystemExit(f"Timed out waiting for Codex App Server to stop{suffix}")

try:
    (Path.home() / ".codex" / "app-server-control" / "app-server-control.sock").unlink()
except FileNotFoundError:
    pass
detail = managed.stdout.strip()
if detail:
    print(detail)
print(f"Codex App Server stopped and verified; fallback_terminated={stopped}")
'''
HOST_APP_SERVER_START_SCRIPT = r'''
import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

codex_bin = Path.home() / ".local" / "bin" / "codex"
if not codex_bin.is_file() or not os.access(codex_bin, os.X_OK):
    codex_bin = Path(shutil.which("codex") or "")
if not codex_bin.is_file():
    raise SystemExit("Codex CLI not found; expected ~/.local/bin/codex or a PATH entry")
codex = str(codex_bin)

remote_control = subprocess.run(
    [codex, "app-server", "daemon", "enable-remote-control"],
    text=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
)
if remote_control.returncode != 0:
    raise SystemExit(remote_control.stdout.strip() or "Failed to enable Codex App Server remote control")
result = subprocess.run([codex, "app-server", "daemon", "start"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
if result.returncode != 0:
    raise SystemExit(result.stdout.strip() or "Failed to start Codex App Server")

deadline = time.monotonic() + 15
last_probe = ""
while time.monotonic() < deadline:
    try:
        # Keep the legacy command shape documented for lifecycle checks; use
        # the resolved absolute executable so non-login SSH shells work.
        # ["codex", "app-server", "daemon", "version"]
        probe = subprocess.run(
            [codex, "app-server", "daemon", "version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=3,
        )
        last_probe = probe.stdout.strip()
        if probe.returncode == 0:
            payload = json.loads(last_probe)
            socket_path = payload.get("socketPath")
            socket_ready = False
            if socket_path:
                try:
                    socket_ready = stat.S_ISSOCK(Path(socket_path).stat().st_mode)
                except FileNotFoundError:
                    pass
            if payload.get("status") == "running" and socket_ready:
                detail = result.stdout.strip()
                if detail:
                    print(detail)
                print(f"Codex App Server ready: {socket_path}")
                break
    except (json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        last_probe = str(exc)
    time.sleep(0.1)
else:
    suffix = f": {last_probe}" if last_probe else ""
    raise SystemExit(f"Timed out waiting for Codex App Server readiness{suffix}")
'''

app = FastAPI(title="Codex Provider Console", version=APP_VERSION, docs_url=None, redoc_url=None)

AUTH_ENABLED = os.environ.get("PANEL_AUTH_ENABLED", "true").lower() not in {"0", "false", "no"}
PANEL_USERNAME = os.environ.get("PANEL_USERNAME", "")
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "")
SESSION_SECRET = os.environ.get("PANEL_SESSION_SECRET", "")
COOKIE_SECURE = os.environ.get("PANEL_COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}
PANEL_BIND = os.environ.get("PANEL_BIND", "127.0.0.1")
SESSION_COOKIE = "codex_panel_session"
SESSION_MAX_AGE = 12 * 60 * 60
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").strip()
SMTP_FROM = os.environ.get("SMTP_FROM", "").strip()
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "Codex 控制台").strip()
SMTP_USE_SSL = os.environ.get("SMTP_USE_SSL", "1").lower() in {"1", "true", "yes"}
try:
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
except ValueError:
    SMTP_PORT = 0
RECOVERY_CODE_LIFETIME = 10 * 60
RECOVERY_GRANT_LIFETIME = 5 * 60
RECOVERY_LOCK = threading.Lock()
RECOVERY_PENDING: dict[str, dict] = {}
RECOVERY_SEND_EVENTS: dict[str, list[float]] = {}
LOGIN_TIMEOUT_SECONDS = 10
LOGIN_FAILURE_WINDOW_SECONDS = 5 * 60
try:
    LOGIN_MAX_FAILURES = max(1, int(os.environ.get("PANEL_LOGIN_MAX_FAILURES", "10")))
except ValueError:
    LOGIN_MAX_FAILURES = 10
LOGIN_FAILURES_LOCK = threading.Lock()
LOGIN_FAILURES: dict[str, list[float]] = {}


def _read_auth_overrides() -> dict:
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")) if SETTINGS_PATH.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


_AUTH_OVERRIDES = _read_auth_overrides()
if _AUTH_OVERRIDES.get("panel_username"):
    PANEL_USERNAME = str(_AUTH_OVERRIDES["panel_username"])
if _AUTH_OVERRIDES.get("panel_password_hash"):
    PANEL_PASSWORD = ""
if _AUTH_OVERRIDES.get("panel_session_secret"):
    SESSION_SECRET = str(_AUTH_OVERRIDES["panel_session_secret"])
if "panel_cookie_secure" in _AUTH_OVERRIDES:
    COOKIE_SECURE = bool(_AUTH_OVERRIDES["panel_cookie_secure"])


AUTH_SERVICE = AuthService(
    username=PANEL_USERNAME,
    password=PANEL_PASSWORD,
    session_secret=SESSION_SECRET,
    settings_path=SETTINGS_PATH,
    session_max_age=SESSION_MAX_AGE,
    failure_window_seconds=LOGIN_FAILURE_WINDOW_SECONDS,
    max_failures=LOGIN_MAX_FAILURES,
)


def current_password_matches(password: str) -> bool:
    return AUTH_SERVICE.current_password_matches(password)

def recovery_email_ready() -> bool:
    return bool(SMTP_HOST and 1 <= SMTP_PORT <= 65535 and SMTP_USERNAME and SMTP_PASSWORD and SMTP_FROM)


def masked_recovery_email(email: str) -> str:
    if not email or "@" not in email:
        return ""
    name, domain = email.rsplit("@", 1)
    return name[:1] + "***@" + domain


def recovery_code_digest(code: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{code}".encode("utf-8")).hexdigest()


def new_recovery_code() -> tuple[str, dict]:
    code = f"{secrets.randbelow(100_000_000):08d}"
    salt = secrets.token_urlsafe(24)
    return code, {
        "salt": salt,
        "digest": recovery_code_digest(code, salt),
        "expires": time.time() + RECOVERY_CODE_LIFETIME,
        "attempts": 0,
    }


def recovery_send_allowed(action: str, client_key: str) -> bool:
    now = time.monotonic()
    with RECOVERY_LOCK:
        for key, limit in ((f"{action}:global", 5), (f"{action}:client:{client_key}", 3)):
            recent = [sent for sent in RECOVERY_SEND_EVENTS.get(key, []) if now - sent < 15 * 60]
            RECOVERY_SEND_EVENTS[key] = recent
            if len(recent) >= limit:
                return False
        for key in (f"{action}:global", f"{action}:client:{client_key}"):
            RECOVERY_SEND_EVENTS[key].append(now)
    return True


def send_recovery_email(recipient: str, subject: str, body: str) -> None:
    if not recovery_email_ready():
        raise HTTPException(503, "邮件找回尚未配置，请通过服务器 SSH 恢复")
    message = EmailMessage()
    message["From"] = f"{SMTP_FROM_NAME} <{SMTP_FROM}>" if SMTP_FROM_NAME else SMTP_FROM
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    try:
        client = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10) if SMTP_USE_SSL else smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10)
        with client:
            if not SMTP_USE_SSL:
                client.starttls()
            client.login(SMTP_USERNAME, SMTP_PASSWORD)
            client.send_message(message)
    except (OSError, smtplib.SMTPException) as error:
        raise HTTPException(502, "邮件发送失败，请稍后重试") from error


DEVICE_LOGIN_LOCK = threading.Lock()
DEVICE_LOGIN: DeviceLoginSession | None = None


from provider_schemas import (LoginRequest, ModelDiagnosticRequest, ModelEntry, Provider, ProviderDiagnosticRequest, UpstreamModelFetch)
from model_diagnostic_domain import validate_model_diagnostic_response


def auth_configured() -> bool:
    return AUTH_SERVICE.auth_configured()


def session_token(username: str) -> str:
    return AUTH_SERVICE.session_token(username)


def valid_session(token: str | None) -> bool:
    return AUTH_SERVICE.valid_session(token)


def login_client_key(request: Request) -> str:
    """Return a best-effort client key without trusting forwarded headers."""
    return AUTH_SERVICE.client_key(request.client.host if request.client else None)


def login_is_rate_limited(client_key: str, now: float | None = None) -> bool:
    return AUTH_SERVICE.login_is_rate_limited(client_key, now)


def record_failed_login(client_key: str, now: float | None = None) -> None:
    AUTH_SERVICE.record_failed_login(client_key, now)


def clear_failed_logins(client_key: str) -> None:
    AUTH_SERVICE.clear_failed_logins(client_key)


from web.pages import LOGIN_HTML, index as render_index


@app.middleware("http")
async def authentication(request: Request, call_next):
    if not AUTH_ENABLED or request.url.path in {
        "/login", "/logout", "/api/password-reset/request",
        "/api/password-reset/verify", "/api/password-reset/complete",
        "/api/totp-password-reset/verify", "/api/totp-password-reset/complete",
    }:
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
def login(credentials: LoginRequest, request: Request) -> JSONResponse:
    if not AUTH_ENABLED:
        return JSONResponse({"authenticated": True})
    if not auth_configured():
        raise HTTPException(503, "Panel authentication is not configured")
    client_key = login_client_key(request)
    if login_is_rate_limited(client_key):
        raise HTTPException(429, "登录尝试过多，请 5 分钟后重试")
    if not (hmac.compare_digest(credentials.username, PANEL_USERNAME) and current_password_matches(credentials.password)):
        record_failed_login(client_key)
        raise HTTPException(401, "用户名或密码错误")
    clear_failed_logins(client_key)
    response = JSONResponse({"authenticated": True})
    response.set_cookie(SESSION_COOKIE, session_token(PANEL_USERNAME), max_age=SESSION_MAX_AGE, httponly=True, samesite="strict", secure=COOKIE_SECURE, path="/")
    return response


@app.post("/logout")
def logout() -> JSONResponse:
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


PROVIDER_STORE = ProviderStore(PROFILE_PATH, SETTINGS_PATH, normalize_profile)


def read_profiles() -> dict[str, dict]:
    try:
        return PROVIDER_STORE.read_profiles()
    except ValueError as exc:
        raise HTTPException(500, str(exc)) from exc


def panel_settings() -> dict:
    try:
        return PROVIDER_STORE.panel_settings()
    except ValueError as exc:
        raise HTTPException(500, str(exc)) from exc


def write_private(path: Path, text: str) -> None:
    atomic_write_private(path, text)


BACKUP_SERVICE = BackupService(
    codex_home=CODEX_HOME,
    backup_root=BACKUP_ROOT,
    metadata_name=BACKUP_METADATA_NAME,
    backup_id_pattern=BACKUP_ID,
    config_path=CONFIG_PATH,
    profile_path=PROFILE_PATH,
    settings_path=SETTINGS_PATH,
    auth_path=AUTH_PATH,
    common_config_path=COMMON_CONFIG_PATH,
    session_index_path=SESSION_INDEX_PATH,
    write_private=write_private,
)


def backup_state(include_sessions: bool = False, reason: str | None = None) -> tuple[str, Path]:
    return BACKUP_SERVICE.backup_state(include_sessions=include_sessions, reason=reason)


def prune_provider_switch_backups(retain: int = PROVIDER_SWITCH_BACKUP_RETENTION) -> list[str]:
    return BACKUP_SERVICE.prune_provider_switch_backups(retain)


def prune_backup_history(retain_complete: int = 1, retain_before_restore: int = 1) -> list[str]:
    return BACKUP_SERVICE.prune_history(
        retain_complete=retain_complete,
        retain_before_restore=retain_before_restore,
    )


def restore_session_state(backup_dir: Path) -> None:
    BACKUP_SERVICE.restore_session_state(backup_dir)


def backup_directory(backup_id: str) -> Path:
    try:
        return BACKUP_SERVICE.backup_directory(backup_id)
    except ValueError as exc:
        raise HTTPException(422, "\u65e0\u6548\u7684\u5907\u4efd\u6807\u8bc6") from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, "\u5907\u4efd\u4e0d\u5b58\u5728") from exc


def restore_backup_state(backup_dir: Path) -> list[str]:
    restored = BACKUP_SERVICE.restore_state(backup_dir)
    audit("backup_restored", backup_id=backup_dir.name, detail=",".join(restored))
    return restored


PROVIDER_SERVICE = ProviderService(
    store=PROVIDER_STORE,
    profile_path=PROFILE_PATH,
    write_private=write_private,
    create_backup=backup_state,
)


def audit(action: str, **details: str | bool | None) -> None:
    payload = {"timestamp": datetime.now(timezone.utc).isoformat(), "action": action, **details}
    with AUDIT_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    os.chmod(AUDIT_PATH, 0o600)


def session_catalog() -> SessionCatalog:
    return SessionCatalog(
        SessionCatalogPaths(
            codex_home=CODEX_HOME,
            sessions_path=SESSIONS_PATH,
            archived_sessions_path=ARCHIVED_SESSIONS_PATH,
            session_index_path=SESSION_INDEX_PATH,
            thread_id=THREAD_ID,
            thread_id_in_text=THREAD_ID_IN_TEXT,
        )
    )


def session_id_from_path(path: Path) -> str | None:
    return session_catalog().session_id_from_path(path)


def session_index_titles() -> dict[str, str]:
    return session_catalog().index_titles()


def session_index_records() -> dict[str, dict]:
    return session_catalog().index_records()


def app_server_thread_states() -> dict[str, bool] | None:
    return session_catalog().app_server_thread_states()


def read_session_summary(path: Path, indexed_title: str = "") -> dict:
    return session_catalog().read_summary(path, indexed_title)


def list_server_sessions() -> list[dict]:
    return session_catalog().list_active()


def list_archived_server_sessions() -> list[dict]:
    return session_catalog().list_archived()


def wait_for_session_archive_state(thread_id: str, archived: bool, timeout_seconds: float = 5.0) -> None:
    """Wait until App Server storage and the panel listing agree on archive state."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        thread_states = app_server_thread_states()
        sessions = list_archived_server_sessions() if archived else list_server_sessions()
        listed = any(item["id"] == thread_id for item in sessions)
        registered = thread_states is not None and thread_states.get(thread_id) is archived
        if registered and listed:
            return
        if time.monotonic() >= deadline:
            action = "归档" if archived else "取消归档"
            raise RuntimeError(f"Codex 已接受{action}命令，但服务器会话状态尚未完成同步")
        time.sleep(0.2)


def confirm_session_archive_in_background(thread_id: str, archived: bool) -> None:
    """Confirm eventual App Server index sync without blocking the HTTP response."""
    def confirm() -> None:
        try:
            wait_for_session_archive_state(thread_id, archived, timeout_seconds=10.0)
            audit("server_session_archive_sync_completed", thread_id=thread_id, archived=archived)
        except RuntimeError as exc:
            audit("server_session_archive_sync_failed", thread_id=thread_id, archived=archived, detail=str(exc))

    threading.Thread(
        target=confirm,
        name=f"session-archive-sync-{thread_id[:8]}",
        daemon=True,
    ).start()


def active_config_provider() -> str | None:
    return PROVIDER_STORE.active_config_provider(CONFIG_PATH)


def active_provider() -> str | None:
    """Return the active panel profile, not Codex's stable session identity."""
    return PROVIDER_STORE.active_provider(CONFIG_PATH)


def active_default_model() -> str | None:
    """Read the model that Codex most recently persisted as its default."""
    return PROVIDER_STORE.active_default_model(CONFIG_PATH)


def public_profile(profile: dict) -> dict:
    # ProviderStore keeps the source-level redaction contract visible here:
    # result["has_bearer_token"] is metadata only; credentials are removed
    # with result.pop("bearer_token", None) and result.pop("auth_contents", None).
    return PROVIDER_STORE.public_profile(profile)


def validate_auth_snapshot(contents: str) -> None:
    try:
        parsed = json.loads(contents)
    except json.JSONDecodeError as exc:
        raise HTTPException(422, "The captured authentication snapshot is invalid JSON") from exc
    if parsed.get("auth_mode") != "chatgpt":
        raise HTTPException(422, "The current auth.json is not a ChatGPT login session")


AUTH_SECRET_FIELD = re.compile(r"(?:token|secret|api[_-]?key|access|refresh|cookie|authorization|credential)", re.IGNORECASE)


def redacted_auth_snapshot(contents: str) -> str:
    """Return account metadata while masking reusable authentication material."""
    try:
        payload = json.loads(contents)
    except (TypeError, json.JSONDecodeError):
        return "官方登录认证已保存，但 auth.json 不是可展示的 JSON。"

    def redact(value: object, field: str = "") -> object:
        if field and AUTH_SECRET_FIELD.search(field):
            return "***"
        if isinstance(value, dict):
            return {str(key): redact(item, str(key)) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value

    return json.dumps(redact(payload), ensure_ascii=False, indent=2)




def config_text() -> str:
    return CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else ""


MODEL_DIAGNOSTIC_TIMEOUT_SECONDS = 45
MODEL_DIAGNOSTIC_TIMEOUT_ATTEMPTS = 2
MODEL_DIAGNOSTIC_MAX_MODELS = 30
MODEL_DIAGNOSTIC_MAX_SECONDS = 180
MODEL_DIAGNOSTIC_JOB_TTL_SECONDS = 3600
MODEL_DIAGNOSTIC_MAX_JOBS = 30


from infrastructure.host_gateway import HostGateway
from infrastructure.upstream_http import (
    UpstreamModelFetchError,
    fetch_upstream_models as fetch_upstream_models_http,
    test_model_request,
    test_model_request_via_relay as test_model_request_via_relay_http,
    test_profile,
)


def test_model_request_via_relay(
    profile: dict, test_model: str, *, timeout_seconds: int = MODEL_DIAGNOSTIC_TIMEOUT_SECONDS
) -> dict:
    return test_model_request_via_relay_http(RELAY_BASE_URL, test_model, timeout_seconds=timeout_seconds)


def fetch_upstream_models(request: UpstreamModelFetch) -> dict:
    try:
        result = fetch_upstream_models_http(request.base_url, request.bearer_token)
    except UpstreamModelFetchError as exc:
        raise HTTPException(422, str(exc)) from exc
    audit("upstream_models_fetched", detail=f"{len(result['models'])} models")
    return result

DIAGNOSTIC_SERVICE = DiagnosticService(
    normalize_profile=normalize_profile,
    validate_auth_snapshot=validate_auth_snapshot,
    fetch_upstream_models=fetch_upstream_models_http,
    test_model_request=test_model_request,
)


def diagnose_profile(
    profile: dict,
    progress: Callable[[int, str], None] | None = None,
    on_check: Callable[[dict[str, str]], None] | None = None,
) -> dict:
    return DIAGNOSTIC_SERVICE.diagnose(profile, progress=progress, on_check=on_check)

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
    security_warnings: list[str] = []
    if PANEL_BIND in {"0.0.0.0", "::"}:
        security_warnings.append("控制台正在监听网络地址。请仅通过 HTTPS 反向代理访问，并在防火墙中限制面板端口。")
    if not COOKIE_SECURE:
        security_warnings.append("会话 Cookie 未启用 Secure 属性；通过 HTTPS 反向代理访问时，请设置 PANEL_COOKIE_SECURE=true 后重启。")
    return {
        "active_provider": active_provider(),
        "auth_keys": auth_keys,
        "has_configured_bearer_token": any("bearer_token" in line for line in config.splitlines()),
        "environment_note": "Shell environment variables are not persisted by Codex and cannot be reliably inspected from this isolated console.",
        "warnings": warnings,
        "security_warnings": security_warnings,
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


def docker_host_gateway() -> str:
    """Resolve Docker's IPv4 default gateway without trusting a request value."""
    try:
        for line in Path("/proc/net/route").read_text(encoding="ascii").splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 3 and fields[1] == "00000000":
                gateway = fields[2]
                return ".".join(str(int(gateway[index : index + 2], 16)) for index in range(6, -1, -2))
    except OSError:
        pass
    raise RuntimeError("无法确定 Docker 宿主机地址")


def ssh_client_environment() -> dict[str, str]:
    return nss_environment(USER_HOME)


def ssh_host_key_options(gateway: str) -> list[str]:
    return verify_ssh_host_key(USER_HOME / ".ssh" / "known_hosts", PANEL_SSH_HOST_ALIAS, gateway, environment=ssh_client_environment())


def host_ssh_command(gateway: str) -> list[str]:
    return build_host_ssh_command(DEPLOYMENT_KEY_PATH, DEPLOY_USER, gateway, USER_HOME / ".ssh" / "known_hosts", PANEL_SSH_HOST_ALIAS, environment=ssh_client_environment())


def host_gateway() -> HostGateway:
    return HostGateway(
        deployment_key=DEPLOYMENT_KEY_PATH,
        deploy_user=DEPLOY_USER,
        user_home=USER_HOME,
        host_user_home=HOST_USER_HOME_PATH,
        host_codex_home=HOST_CODEX_HOME_PATH,
        command=host_ssh_command,
        environment=ssh_client_environment,
    )


def run_host_gateway_script(script: str, operation: str, timeout: int = 45) -> str:
    return host_gateway().run(script, operation, timeout)


def run_host_app_server_control(action: Literal["start", "stop"]) -> str:
    """Use the verified gateway for one fixed App Server action."""
    script = HOST_APP_SERVER_START_SCRIPT if action == "start" else HOST_APP_SERVER_STOP_SCRIPT
    return run_host_gateway_script(script, f"Codex App Server {action}")

def host_panel_project_path() -> str:
    if HOST_USER_HOME_PATH and Path(HOST_USER_HOME_PATH).is_absolute():
        return str(Path(HOST_USER_HOME_PATH) / "codex-provider-console")
    raise RuntimeError("未找到宿主机控制台目录，无法管理反向代理")


def run_host_reverse_proxy_script(script: str, operation: str, timeout: int = 120) -> str:
    if not DEPLOYMENT_KEY_PATH.is_file():
        raise RuntimeError("缺少面板部署密钥；请先在健康检查中部署密钥")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", DEPLOY_USER):
        raise RuntimeError("部署用户名无效，无法管理反向代理")
    gateway = docker_host_gateway()
    try:
        result = subprocess.run(
            host_ssh_command(gateway),
            capture_output=True,
            text=True,
            input=script,
            timeout=timeout,
            check=False,
            env=ssh_client_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"宿主机反向代理{operation}超时") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "宿主机未提供失败原因").strip()
        raise RuntimeError(f"宿主机反向代理{operation}失败：{detail[-600:]}")
    return (result.stdout or result.stderr).strip()


from infrastructure.reverse_proxy_gateway import (
    check_reverse_proxy_upstream,
    reverse_proxy_apply_script,
    reverse_proxy_certificate_preflight_script,
    reverse_proxy_delete_script,
    reverse_proxy_disable_secure_mode_script,
    reverse_proxy_secure_mode_script,
    reverse_proxy_status_script,
    validate_reverse_proxy_input,
)

def host_codex_home_path() -> str:
    """Return the host-side Codex home used by the Desktop App Server."""
    try:
        return resolve_host_codex_home(HOST_CODEX_HOME_PATH, HOST_USER_HOME_PATH)
    except ValueError as exc:
        raise RuntimeError(f"{exc}，无法同步删除云端会话") from exc


def host_session_delete_script(thread_id: str, codex_home: str) -> str:
    """Build the small host-side script used for session deletion."""
    delete_args = remote_session_delete_args(thread_id)
    return f'''import json
import os
import subprocess
from pathlib import Path

session_id = {thread_id!r}
codex_home = {codex_home!r}
home = Path.home()
candidates = [
    home / ".local" / "bin" / "codex",
    home / ".codex" / "bin" / "codex",
    home / ".codex" / "packages" / "standalone" / "current" / "bin" / "codex",
    Path("/usr/local/bin/codex"),
]
binary = next((str(path) for path in candidates if path.is_file() and os.access(path, os.X_OK)), "codex")
env = os.environ.copy()
env["CODEX_HOME"] = codex_home
# Route deletion through the running managed App Server. Besides removing the
# remote rollout and index, this emits thread/deleted to connected desktop
# clients so only the matching remote cache entry disappears.
result = subprocess.run([binary, *{delete_args!r}], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
index_path = Path(codex_home) / "session_index.jsonl"
removed_index = False
if index_path.is_file():
    kept = []
    for line in index_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)
            continue
        if str(record.get("id") or "").lower() == session_id:
            removed_index = True
        else:
            kept.append(line)
    if removed_index:
        temp_path = index_path.with_suffix(".jsonl.tmp")
        temp_path.write_text("".join(kept), encoding="utf-8")
        os.replace(temp_path, index_path)
if result.returncode != 0 and not removed_index:
    raise SystemExit(result.stdout.strip() or "Codex delete failed")
message = result.stdout.strip()
if removed_index:
    message = (message + "\\n" if message else "") + "Removed stale session-index entry."
print(message)
'''


def run_host_session_delete(thread_id: str) -> str:
    """Delete via the host App Server through the verified gateway."""
    return run_host_gateway_script(host_session_delete_script(thread_id, host_codex_home_path()), "Codex 删除会话")

def host_session_archive_script(thread_id: str, codex_home: str, archived: bool) -> str:
    """Build a host-side archive or unarchive command using the managed daemon."""
    command_args = remote_session_archive_args(thread_id) if archived else remote_session_unarchive_args(thread_id)
    return f'''import os
import subprocess
from pathlib import Path

codex_home = {codex_home!r}
home = Path.home()
candidates = [
    home / ".local" / "bin" / "codex",
    home / ".codex" / "bin" / "codex",
    home / ".codex" / "packages" / "standalone" / "current" / "bin" / "codex",
    Path("/usr/local/bin/codex"),
]
binary = next((str(path) for path in candidates if path.is_file() and os.access(path, os.X_OK)), "codex")
env = os.environ.copy()
env["CODEX_HOME"] = codex_home
result = subprocess.run([binary, *{command_args!r}], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
if result.returncode != 0:
    raise SystemExit(result.stdout.strip() or "Codex archive operation failed")
print(result.stdout.strip())
'''


def run_host_session_archive(thread_id: str, archived: bool) -> str:
    """Archive or unarchive through the verified host gateway."""
    action = "归档会话" if archived else "取消归档会话"
    return run_host_gateway_script(host_session_archive_script(thread_id, host_codex_home_path(), archived), f"Codex {action}")

def migrate_session_provider(
    provider_id: str,
    source_provider: str | None = None,
    apply: bool = True,
    backup_dir: Path | None = None,
) -> dict[str, int]:
    """Preview or apply a migration to Codex's stable session provider ID."""
    database_changes = 0
    if (database := CODEX_HOME / "state_5.sqlite").is_file():
        connection = sqlite3.connect(database, timeout=10)
        try:
            has_threads = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'threads'"
            ).fetchone()
            if has_threads:
                if source_provider:
                    predicate, arguments = "model_provider = ?", (source_provider,)
                else:
                    predicate, arguments = "model_provider IS NOT ?", (provider_id,)
                if apply:
                    row = connection.execute(
                        f"UPDATE threads SET model_provider = ? WHERE {predicate}",
                        (provider_id, *arguments),
                    )
                    database_changes = max(row.rowcount, 0)
                    connection.commit()
                else:
                    database_changes = int(connection.execute(f"SELECT COUNT(*) FROM threads WHERE {predicate}", arguments).fetchone()[0])
        finally:
            connection.close()

    rollout_files = 0
    rollout_records = 0
    for directory in (SESSIONS_PATH, ARCHIVED_SESSIONS_PATH):
        if not directory.is_dir():
            continue
        for path in directory.rglob("*.jsonl"):
            original = path.read_text(encoding="utf-8", errors="replace")
            lines: list[str] = []
            changed = 0
            for line in original.splitlines(keepends=True):
                newline = "\n" if line.endswith("\n") else ""
                try:
                    record = json.loads(line.rstrip("\n"))
                except json.JSONDecodeError:
                    lines.append(line)
                    continue
                payload = record.get("payload")
                matches_source = isinstance(payload, dict) and (not source_provider or payload.get("model_provider") == source_provider)
                if record.get("type") == "session_meta" and isinstance(payload, dict) and matches_source and payload.get("model_provider") != provider_id:
                    payload["model_provider"] = provider_id
                    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + newline
                    changed += 1
                lines.append(line)
            if changed and apply:
                if backup_dir:
                    relative = path.relative_to(CODEX_HOME)
                    snapshot = backup_dir / relative
                    snapshot.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, snapshot)
                    os.chmod(snapshot, 0o600)
                temporary = path.with_name(path.name + ".provider-switch.tmp")
                temporary.write_text("".join(lines), encoding="utf-8")
                temporary.chmod(path.stat().st_mode)
                temporary.replace(path)
            if changed:
                rollout_files += 1
                rollout_records += changed
    return {"threads": database_changes, "rollout_files": rollout_files, "rollout_records": rollout_records}


def cli_version_number(value: str) -> str:
    match = re.search(r"(?<!\d)(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)", value or "")
    return match.group(1) if match else ""

def fetch_latest_cli_version() -> str:
    request = urllib.request.Request(
        CODEX_LATEST_RELEASE_URL,
        headers={"User-Agent": "codex-provider-console-health"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.load(response)
    version = cli_version_number(str(payload.get("tag_name", "")))
    if not version:
        raise ValueError("官方版本信息格式无法识别")
    return version


def inspect_host_app_server() -> tuple[str, str]:
    """Check the managed host daemon without starting or stopping it."""
    if not DEPLOYMENT_KEY_PATH.is_file():
        return "warning", "缺少面板部署密钥，无法检查宿主机 App Server"
    script = r'''
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

codex = Path.home() / ".local" / "bin" / "codex"
if not codex.is_file() or not os.access(codex, os.X_OK):
    codex = Path(shutil.which("codex") or "")
if not codex.is_file():
    raise SystemExit("Codex CLI not found")
result = subprocess.run(
    [str(codex), "app-server", "daemon", "version"],
    capture_output=True, text=True, timeout=5, check=False,
)
if result.returncode:
    raise SystemExit((result.stderr or result.stdout or "daemon version failed").strip())
payload = json.loads(result.stdout)
socket_path = payload.get("socketPath")
try:
    socket_ready = bool(socket_path and stat.S_ISSOCK(Path(socket_path).stat().st_mode))
except OSError:
    socket_ready = False
print(json.dumps({"status": payload.get("status"), "socket_ready": socket_ready,
                  "version": payload.get("appServerVersion") or payload.get("managedCodexVersion")}))
'''
    try:
        gateway = docker_host_gateway()
        result = subprocess.run(
            host_ssh_command(gateway), input=script, capture_output=True, text=True,
            timeout=15, check=False, env=ssh_client_environment(),
        )
        if result.returncode:
            detail = (result.stderr or result.stdout or "宿主机未提供失败原因").strip()
            return "fail", f"无法读取 App Server 状态：{detail[-300:]}"
        payload = json.loads(result.stdout)
    except (OSError, RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return "warning", f"无法检查宿主机 App Server：{str(exc)[-300:]}"
    if payload.get("status") != "running" or not payload.get("socket_ready"):
        return "fail", f"状态：{payload.get('status') or '未知'}；控制套接字{'就绪' if payload.get('socket_ready') else '不可用'}"
    return "pass", f"运行中；控制套接字就绪；版本 {payload.get('version') or '未知'}"


def inspect_session_storage() -> tuple[str, str]:
    """Check that registered active threads still have rollout files."""
    states = app_server_thread_states()
    if states is None:
        return "warning", "无法读取 App Server 会话数据库"
    active_ids = {thread_id for thread_id, archived in states.items() if not archived}
    try:
        rollout_ids = {
            thread_id
            for path in SESSIONS_PATH.rglob("*.jsonl") if SESSIONS_PATH.is_dir()
            if (thread_id := session_id_from_path(path))
        }
    except OSError as exc:
        return "fail", f"无法读取会话文件：{exc}"
    missing = active_ids - rollout_ids
    if missing:
        return "fail", f"{len(missing)} 个活动会话缺少历史文件（已登记 {len(active_ids)} 个）"
    return "pass", f"{len(active_ids)} 个活动会话均有历史文件；这是存储检查，不包含客户端加载耗时"

def health_dependencies() -> HealthDependencies:
    return HealthDependencies(
        codex_home=CODEX_HOME,
        config_path=CONFIG_PATH,
        auth_path=AUTH_PATH,
        deploy_user=DEPLOY_USER,
        auth_enabled=AUTH_ENABLED,
        executable_user_cli_path=executable_user_cli_path,
        fetch_latest_cli_version=fetch_latest_cli_version,
        cli_version_number=cli_version_number,
        deployment_key_status=deployment_key_status,
        inspect_host_app_server=inspect_host_app_server,
        inspect_session_storage=inspect_session_storage,
        config_text=config_text,
        auth_configured=auth_configured,
        read_profiles=read_profiles,
        active_provider=active_provider,
        normalize_profile=normalize_profile,
        diagnose_profile=diagnose_profile,
        audit=audit,
    )


def health_check(on_check: Callable[[dict[str, str]], None] | None = None) -> dict:
    return HealthService(health_dependencies()).run(on_check)


def health_check_plan() -> list[dict[str, str]]:
    return HealthService(health_dependencies()).plan()


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


PROVIDER_SWITCH_SERVICE = ProviderSwitchService(
    ProviderSwitchDependencies(
        panel_settings=panel_settings,
        read_profiles=read_profiles,
        active_provider=active_provider,
        active_default_model=active_default_model,
        diagnose_profile=diagnose_profile,
        backup_state=backup_state,
        config_text=config_text,
        run_host_app_server_control=run_host_app_server_control,
        migrate_session_provider=migrate_session_provider,
        read_auth_overrides=_read_auth_overrides,
        write_private=write_private,
        restore_session_state=restore_session_state,
        audit=audit,
        prune_provider_switch_backups=prune_provider_switch_backups,
        app_server_lock=APP_SERVER_LOCK,
        common_config_path=COMMON_CONFIG_PATH,
        config_path=CONFIG_PATH,
        auth_path=AUTH_PATH,
        profile_path=PROFILE_PATH,
        settings_path=SETTINGS_PATH,
        session_provider_id=SESSION_PROVIDER_ID,
        host_relay_base_url=HOST_RELAY_BASE_URL,
    )
)


def switch_provider(
    provider_id: str,
    verify: bool = True,
    model_override: str | None = None,
    progress: Callable[[int, str], None] | None = None,
) -> dict:
    """Switch providers serially so auth/config snapshots cannot interleave.

    A switch replaces the shared Codex config and auth files.  Serializing the
    whole operation is required for multiple official-login profiles: two
    concurrent switches could otherwise validate different snapshots and then
    write the wrong account last.
    """
    if not PROVIDER_SWITCH_LOCK.acquire(blocking=False):
        raise HTTPException(409, "Another provider switch is already in progress")
    try:
        return PROVIDER_SWITCH_SERVICE.run(
            provider_id,
            verify=verify,
            model_override=model_override,
            progress=progress,
        )
    finally:
        PROVIDER_SWITCH_LOCK.release()


@OPERATIONS_ROUTER.get("/api/status")
def status() -> dict:
    model = None
    if CONFIG_PATH.exists():
        match = re.search(r'^\s*model\s*=\s*"([^"]+)"', CONFIG_PATH.read_text(encoding="utf-8"), re.M)
        model = match.group(1) if match else None
    return {"active_provider": active_provider(), "model": model, "profiles": [public_profile(p) for p in read_profiles().values()], "preflight": preflight(), "settings": panel_settings()}


class PanelSettings(BaseModel):
    provider_switching_enabled: bool = True
    reverse_proxy: dict = Field(default_factory=dict)
    # Internal selection state. Browser settings forms do not need to send it.
    active_provider_id: str | None = Field(default=None, pattern=r"^(|[a-zA-Z0-9_-]{1,48})$")


class AccountPasswordRequest(BaseModel):
    new_password: str = Field(min_length=8, max_length=256)


class AccountUsernameRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")


class RecoveryEmailRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RecoveryCodeRequest(BaseModel):
    code: str = Field(pattern=r"^[0-9]{8}$")


class ResetPasswordRequest(BaseModel):
    reset_token: str = Field(min_length=32, max_length=128)
    new_password: str = Field(min_length=8, max_length=256)


class TOTPCodeRequest(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")


class TOTPRecoveryRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    code: str = Field(pattern=r"^\d{6}$")


class TOTPResetRequest(BaseModel):
    reset_token: str = Field(min_length=32, max_length=128)
    new_password: str = Field(min_length=8, max_length=256)


class ReverseProxyApplyRequest(BaseModel):
    domain: str = Field(min_length=1, max_length=253)
    certificate_pem: str = Field(min_length=1, max_length=200_000)
    private_key_pem: str = Field(min_length=1, max_length=200_000)


class CommonConfigRequest(BaseModel):
    contents: str = Field(default="", max_length=200000)


class SessionMigrationRequest(BaseModel):
    target_provider: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,48}$")
    source_provider: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,48}$")
    apply: bool = False


@ACCOUNT_ROUTER.get("/api/settings")
def get_settings() -> dict:
    return panel_settings()


@ACCOUNT_ROUTER.post("/api/settings")
def save_settings(request: PanelSettings) -> dict:
    settings = _read_auth_overrides()
    settings["provider_switching_enabled"] = request.provider_switching_enabled
    settings["reverse_proxy"] = request.reverse_proxy
    if request.active_provider_id is not None:
        settings["active_provider_id"] = request.active_provider_id
    write_private(SETTINGS_PATH, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
    audit("provider_switching_setting_changed", enabled=request.provider_switching_enabled)
    return settings


@ACCOUNT_ROUTER.get("/api/account")
def get_account_settings() -> dict:
    return {
        "username": PANEL_USERNAME,
        "cookie_secure": COOKIE_SECURE,
        "panel_bind": PANEL_BIND,
        "public_bind_warning": PANEL_BIND not in {"127.0.0.1", "localhost", "::1"},
        "restart_required_for_bind": True,
    }


@ACCOUNT_ROUTER.post("/api/account")
@ACCOUNT_ROUTER.post("/api/account/password")
def save_account_password(request: AccountPasswordRequest) -> dict:
    global PANEL_PASSWORD, SESSION_SECRET
    if current_password_matches(request.new_password):
        raise HTTPException(400, "新密码不能与当前密码相同")
    settings = _read_auth_overrides()
    salt = secrets.token_urlsafe(24)
    settings["panel_password_salt"] = salt
    settings["panel_password_hash"] = hashlib.pbkdf2_hmac(
        "sha256", request.new_password.encode("utf-8"), salt.encode("utf-8"), 210_000
    ).hex()
    settings["panel_session_secret"] = secrets.token_urlsafe(32)
    write_private(SETTINGS_PATH, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
    PANEL_PASSWORD = ""
    SESSION_SECRET = settings["panel_session_secret"]
    AUTH_SERVICE.update_runtime(password=PANEL_PASSWORD, session_secret=SESSION_SECRET)
    with LOGIN_FAILURES_LOCK:
        LOGIN_FAILURES.clear()
    audit("account_password_changed", username=PANEL_USERNAME)
    return {"username": PANEL_USERNAME, "cookie_secure": COOKIE_SECURE, "sessions_invalidated": True}


@ACCOUNT_ROUTER.post("/api/account/username")
def save_account_username(request: AccountUsernameRequest) -> dict:
    global PANEL_USERNAME, SESSION_SECRET
    if request.username == PANEL_USERNAME:
        raise HTTPException(400, "新账号不能与当前账号相同")
    settings = _read_auth_overrides()
    settings["panel_username"] = request.username
    settings["panel_session_secret"] = secrets.token_urlsafe(32)
    write_private(SETTINGS_PATH, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
    PANEL_USERNAME = request.username
    SESSION_SECRET = settings["panel_session_secret"]
    AUTH_SERVICE.update_runtime(username=PANEL_USERNAME, session_secret=SESSION_SECRET)
    with LOGIN_FAILURES_LOCK:
        LOGIN_FAILURES.clear()
    audit("account_username_changed", username=request.username)
    return {"username": PANEL_USERNAME, "sessions_invalidated": True}


def _write_recovery_settings(settings: dict) -> None:
    write_private(SETTINGS_PATH, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")


def _apply_recovery_password(new_password: str) -> None:
    global PANEL_PASSWORD, SESSION_SECRET
    settings = _read_auth_overrides()
    salt = secrets.token_urlsafe(24)
    settings["panel_password_salt"] = salt
    settings["panel_password_hash"] = hashlib.pbkdf2_hmac("sha256", new_password.encode("utf-8"), salt.encode("utf-8"), 210_000).hex()
    settings["panel_session_secret"] = secrets.token_urlsafe(32)
    _write_recovery_settings(settings)
    PANEL_PASSWORD = ""
    SESSION_SECRET = settings["panel_session_secret"]
    AUTH_SERVICE.update_runtime(password=PANEL_PASSWORD, session_secret=SESSION_SECRET)
    with LOGIN_FAILURES_LOCK:
        LOGIN_FAILURES.clear()


RECOVERY_SERVICE = RecoveryService(
    read_settings=_read_auth_overrides,
    write_settings=_write_recovery_settings,
    session_secret=lambda: SESSION_SECRET,
    password_matches=current_password_matches,
    password_reset=_apply_recovery_password,
    send_email=send_recovery_email,
    email_ready=recovery_email_ready,
    audit=audit,
    code_lifetime=RECOVERY_CODE_LIFETIME,
    grant_lifetime=RECOVERY_GRANT_LIFETIME,
)

TOTP_SERVICE = TOTPService(
    read_settings=_read_auth_overrides,
    write_settings=_write_recovery_settings,
    session_secret=lambda: SESSION_SECRET,
    password_matches=current_password_matches,
    password_reset=_apply_recovery_password,
    audit=audit,
)


def recovery_result(operation):
    try:
        return operation()
    except RecoveryError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


def totp_result(operation):
    try:
        return operation()
    except TOTPError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@ACCOUNT_ROUTER.get("/api/account/totp")
def get_totp_status() -> dict:
    return TOTP_SERVICE.status()


@ACCOUNT_ROUTER.post("/api/account/totp/setup")
def setup_totp() -> dict:
    return totp_result(lambda: TOTP_SERVICE.setup(PANEL_USERNAME))


@ACCOUNT_ROUTER.post("/api/account/totp/confirm")
def confirm_totp(details: TOTPCodeRequest) -> dict:
    return totp_result(lambda: TOTP_SERVICE.confirm(details.code))


@ACCOUNT_ROUTER.delete("/api/account/totp")
def delete_totp(details: TOTPCodeRequest) -> dict:
    return totp_result(lambda: TOTP_SERVICE.disable(details.code))


@ACCOUNT_ROUTER.get("/api/account/recovery-email")
def get_recovery_email() -> dict:
    return RECOVERY_SERVICE.status()


@ACCOUNT_ROUTER.post("/api/account/recovery-email/unbind/request")
def request_recovery_email_unbind(request: Request) -> dict:
    return recovery_result(lambda: RECOVERY_SERVICE.request_unbind(login_client_key(request)))


@ACCOUNT_ROUTER.delete("/api/account/recovery-email")
def delete_recovery_email(details: RecoveryCodeRequest) -> dict:
    return recovery_result(lambda: RECOVERY_SERVICE.unbind(details.code))


@ACCOUNT_ROUTER.post("/api/account/recovery-email/request")
def request_recovery_email_binding(details: RecoveryEmailRequest, request: Request) -> dict:
    return recovery_result(lambda: RECOVERY_SERVICE.request_binding(details.email, login_client_key(request)))


@ACCOUNT_ROUTER.post("/api/account/recovery-email/verify")
def verify_recovery_email_binding(details: RecoveryCodeRequest) -> dict:
    return recovery_result(lambda: RECOVERY_SERVICE.verify_binding(details.code))


@ACCOUNT_ROUTER.post("/api/password-reset/request")
def request_password_reset(details: RecoveryEmailRequest, request: Request) -> dict:
    return recovery_result(lambda: RECOVERY_SERVICE.request_reset(details.email, login_client_key(request)))


@ACCOUNT_ROUTER.post("/api/password-reset/verify")
def verify_password_reset(details: RecoveryCodeRequest) -> dict:
    return recovery_result(lambda: RECOVERY_SERVICE.verify_reset(details.code))


@ACCOUNT_ROUTER.post("/api/password-reset/complete")
def complete_password_reset(details: ResetPasswordRequest) -> dict:
    return recovery_result(lambda: RECOVERY_SERVICE.complete_reset(details.reset_token, details.new_password))


@app.post("/api/totp-password-reset/verify")
def verify_totp_password_reset(details: TOTPRecoveryRequest, request: Request) -> dict:
    return totp_result(lambda: TOTP_SERVICE.recover(details.username, PANEL_USERNAME, details.code, login_client_key(request)))


@app.post("/api/totp-password-reset/complete")
def complete_totp_password_reset(details: TOTPResetRequest) -> dict:
    return totp_result(lambda: TOTP_SERVICE.complete_reset(details.reset_token, details.new_password))


@ACCOUNT_ROUTER.get("/api/common-config")
def get_common_config() -> dict:
    contents = COMMON_CONFIG_PATH.read_text(encoding="utf-8") if COMMON_CONFIG_PATH.exists() else remove_provider_sections(config_text())
    return {"contents": contents}


@ACCOUNT_ROUTER.post("/api/common-config/extract")
def extract_common_config() -> dict:
    contents = remove_provider_sections(config_text())
    write_private(COMMON_CONFIG_PATH, contents + ("\n" if contents else ""))
    audit("common_config_extracted")
    return {"contents": contents}


@ACCOUNT_ROUTER.post("/api/common-config")
def save_common_config(request: CommonConfigRequest) -> dict:
    contents = canonicalize_base_config(request.contents.strip())
    try:
        tomllib.loads(contents or "")
    except tomllib.TOMLDecodeError as exc:
        raise HTTPException(422, "通用配置不是有效的 TOML") from exc
    write_private(COMMON_CONFIG_PATH, contents + ("\n" if contents else ""))
    audit("common_config_saved")
    return {"contents": contents}


@PROXY_ROUTER.get("/api/reverse-proxy/status")
def reverse_proxy_status() -> dict:
    """Return deployment state without ever exposing certificate material."""
    try:
        result = json.loads(run_host_reverse_proxy_script(reverse_proxy_status_script(str(panel_settings().get("reverse_proxy", {}).get("domain", "")).strip().lower()), "读取状态", timeout=30))
    except json.JSONDecodeError as exc:
        raise HTTPException(502, "反向代理状态返回格式无效") from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    saved = panel_settings().get("reverse_proxy", {})
    return {
        "configured": bool(result.get("configured")),
        "running": bool(result.get("running")),
        "backend": str(result.get("backend") or saved.get("backend") or ""),
        "secure_mode": PANEL_BIND in {"127.0.0.1", "localhost", "::1"} and bool(COOKIE_SECURE),
        "panel_bind": PANEL_BIND,
        "cookie_secure": bool(COOKIE_SECURE),
        "mode_mismatch": (PANEL_BIND in {"127.0.0.1", "localhost", "::1"}) != bool(COOKIE_SECURE),
        "https_verified": bool(result.get("https_verified")),
        "https_error": str(result.get("https_error", "")),
        "domain": str(result.get("domain") or saved.get("domain", "")),
        "upstream": str(result.get("upstream") or saved.get("upstream", "")),
        "managed_upstream": str(result.get("managed_upstream") or ("127.0.0.1:8787" if result.get("backend") == "system_nginx" else REVERSE_PROXY_UPSTREAM)),
    }


@PROXY_ROUTER.post("/api/reverse-proxy/enable-secure-mode")
def enable_reverse_proxy_secure_mode(request: Request) -> dict:
    saved = panel_settings().get("reverse_proxy", {})
    domain = str(saved.get("domain", "")).strip().lower()
    if not domain:
        raise HTTPException(409, "请先部署 HTTPS 反向代理")
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",", 1)[0].strip().lower()
    request_host = request.headers.get("host", "").split(":", 1)[0].strip().lower()
    if forwarded_proto != "https" or request_host != domain:
        raise HTTPException(409, f"请先通过 https://{domain} 访问控制台，再启用安全模式")
    try:
        result = json.loads(run_host_reverse_proxy_script(reverse_proxy_secure_mode_script(), "启用安全模式", timeout=150))
    except json.JSONDecodeError as exc:
        raise HTTPException(502, "安全模式切换返回格式无效") from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    audit("reverse_proxy_secure_mode_enabled", domain=domain)
    return {"secure_mode": bool(result.get("secure_mode")), "detail": "HTTPS 已验证，面板已切换为本机监听并启用 Secure Cookie。请刷新页面。"}


@PROXY_ROUTER.post("/api/reverse-proxy/disable-secure-mode")
def disable_reverse_proxy_secure_mode(request: Request) -> dict:
    saved = panel_settings().get("reverse_proxy", {})
    domain = str(saved.get("domain", "")).strip().lower()
    if not domain:
        raise HTTPException(409, "请先部署 HTTPS 反向代理")
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",", 1)[0].strip().lower()
    request_host = request.headers.get("host", "").split(":", 1)[0].strip().lower()
    if forwarded_proto != "https" or request_host != domain:
        raise HTTPException(409, f"请通过 https://{domain} 访问控制台后再关闭安全模式")
    try:
        result = json.loads(run_host_reverse_proxy_script(reverse_proxy_disable_secure_mode_script(), "关闭安全模式", timeout=150))
    except json.JSONDecodeError as exc:
        raise HTTPException(502, "关闭安全模式返回格式无效") from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    audit("reverse_proxy_secure_mode_disabled", domain=domain)
    return {"secure_mode": bool(result.get("secure_mode")), "detail": "安全模式已关闭，面板将恢复 8787 公网监听。请刷新页面。"} 


@PROXY_ROUTER.post("/api/reverse-proxy/apply")
def apply_reverse_proxy(request: ReverseProxyApplyRequest) -> dict:
    domain, upstream, certificate_pem, private_key_pem = validate_reverse_proxy_input(
        request.domain, request.certificate_pem, request.private_key_pem
    )
    with REVERSE_PROXY_LOCK:
        try:
            result = json.loads(run_host_reverse_proxy_script(
                reverse_proxy_apply_script(domain, upstream, certificate_pem, private_key_pem),
                "部署",
            ))
        except json.JSONDecodeError as exc:
            raise HTTPException(502, "反向代理部署返回格式无效") from exc
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc
    # Keep only non-sensitive deployment metadata in the panel's settings file.
    settings = _read_auth_overrides()
    settings["reverse_proxy"] = {"domain": domain, "upstream": upstream, "backend": str(result.get("backend", "docker"))}
    write_private(SETTINGS_PATH, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
    audit("reverse_proxy_applied", domain=domain, upstream=upstream)
    return {
        "configured": True,
        "running": bool(result.get("running")),
        "domain": domain,
        "upstream": str(result.get("upstream") or upstream),
        "detail": "已校验证书并部署反向代理。请在云防火墙中仅公开 80/443，并限制面板端口 8787 的直接访问。",
    }


def reverse_proxy_job_snapshot(job_id: str) -> dict:
    with REVERSE_PROXY_JOBS_LOCK:
        job = REVERSE_PROXY_JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "反向代理部署任务不存在或已过期")
        return dict(job)


@PROXY_ROUTER.post("/api/reverse-proxy/apply-progress")
def start_reverse_proxy_apply_progress(request: ReverseProxyApplyRequest) -> dict:
    job_id = uuid.uuid4().hex
    with REVERSE_PROXY_JOBS_LOCK:
        if any(job["status"] == "running" for job in REVERSE_PROXY_JOBS.values()):
            raise HTTPException(409, "已有反向代理部署任务正在运行")
        REVERSE_PROXY_JOBS[job_id] = new_background_job("reverse_proxy", {
            "id": job_id, "status": "running", "progress": 2, "step": 0,
            "stage": "准备校验输入", "detail": "", "warning": "", "result": None,
            "updated_at": time.time(),
        })
        finished = [key for key, job in REVERSE_PROXY_JOBS.items() if job["status"] != "running"]
        for key in finished[:-20]:
            del REVERSE_PROXY_JOBS[key]

    def report(progress: int, step: int, stage: str) -> None:
        with REVERSE_PROXY_JOBS_LOCK:
            REVERSE_PROXY_JOBS[job_id].update(progress=progress, step=step, stage=stage, updated_at=time.time())

    def run() -> None:
        try:
            with REVERSE_PROXY_LOCK:
                report(5, 0, "正在校验输入")
                domain, upstream, certificate, private_key = validate_reverse_proxy_input(
                    request.domain, request.certificate_pem, request.private_key_pem
                )
                report(20, 1, "正在检查上游连接")
                check_reverse_proxy_upstream(upstream)
                report(36, 2, "正在校验证书域名与有效期")
                run_host_reverse_proxy_script(
                    reverse_proxy_certificate_preflight_script(domain, certificate, private_key), "证书预检查", timeout=30
                )
                report(52, 3, "正在校验 Nginx 配置")
                run_host_reverse_proxy_script(
                    reverse_proxy_apply_script(domain, upstream, certificate, private_key, validate_only=True),
                    "配置预检查", timeout=120,
                )
                report(70, 4, "正在部署反向代理")
                deployed = json.loads(run_host_reverse_proxy_script(
                    reverse_proxy_apply_script(domain, upstream, certificate, private_key), "部署", timeout=120
                ))
                settings = _read_auth_overrides()
                settings["reverse_proxy"] = {"domain": domain, "upstream": upstream, "backend": str(deployed.get("backend", "docker"))}
                write_private(SETTINGS_PATH, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
                audit("reverse_proxy_applied", domain=domain, upstream=upstream)
                report(88, 5, "正在验证 HTTPS 访问")
                try:
                    verified = json.loads(run_host_reverse_proxy_script(
                        reverse_proxy_status_script(domain), "HTTPS 验证", timeout=30
                    ))
                    warning = "" if verified.get("https_verified") else str(
                        verified.get("https_error") or "HTTPS 访问尚未通过验证"
                    )
                except (RuntimeError, json.JSONDecodeError) as exc:
                    warning = f"HTTPS 状态暂无法确认：{exc}"
                result = {
                    "configured": True, "running": bool(deployed.get("running")),
                    "domain": domain,
                    "upstream": str(deployed.get("upstream") or upstream),
                    "backend": str(deployed.get("backend") or "docker"),
                    "https_verified": not bool(warning),
                }
            with REVERSE_PROXY_JOBS_LOCK:
                REVERSE_PROXY_JOBS[job_id].update(
                    status="completed", progress=100, step=5, stage="反向代理部署完成",
                    detail="部署已完成；HTTPS 验证未通过。" if warning else "部署与 HTTPS 验证已完成。",
                    warning=warning, result=result, updated_at=time.time(),
                )
        except HTTPException as exc:
            with REVERSE_PROXY_JOBS_LOCK:
                REVERSE_PROXY_JOBS[job_id].update(
                    status="failed", detail=str(exc.detail), updated_at=time.time()
                )
        except Exception as exc:  # pragma: no cover - keep failed jobs visible to the polling client
            with REVERSE_PROXY_JOBS_LOCK:
                REVERSE_PROXY_JOBS[job_id].update(
                    status="failed", detail=str(exc), updated_at=time.time()
                )

    threading.Thread(target=run, name=f"reverse-proxy-{job_id[:8]}", daemon=True).start()
    return reverse_proxy_job_snapshot(job_id)


@PROXY_ROUTER.get("/api/reverse-proxy/jobs/{job_id}")
def get_reverse_proxy_job(job_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(422, "无效的反向代理部署任务标识")
    return reverse_proxy_job_snapshot(job_id)


@PROXY_ROUTER.post("/api/reverse-proxy/delete-progress")
def start_reverse_proxy_delete_progress() -> dict:
    job_id = uuid.uuid4().hex
    with REVERSE_PROXY_JOBS_LOCK:
        if any(job["status"] == "running" for job in REVERSE_PROXY_JOBS.values()):
            raise HTTPException(409, "已有反向代理任务正在运行")
        REVERSE_PROXY_JOBS[job_id] = new_background_job("reverse_proxy", {
            "id": job_id, "status": "running", "progress": 2, "step": 0,
            "stage": "准备删除代理", "detail": "", "warning": "", "result": None,
            "updated_at": time.time(),
        })

    def report(progress: int, step: int, stage: str) -> None:
        with REVERSE_PROXY_JOBS_LOCK:
            REVERSE_PROXY_JOBS[job_id].update(progress=progress, step=step, stage=stage, updated_at=time.time())

    def run() -> None:
        try:
            with REVERSE_PROXY_LOCK:
                report(20, 1, "正在检查当前代理配置")
                saved = _read_auth_overrides().get("reverse_proxy", {})
                domain = str(saved.get("domain", "")).strip().lower()
                if not domain:
                    raise RuntimeError("未找到已保存的反向代理域名")
                report(45, 2, "正在删除 Nginx 配置和 TLS 文件")
                run_host_reverse_proxy_script(reverse_proxy_delete_script(domain), "删除", timeout=45)
                report(75, 3, "正在重载 Nginx")
                settings = _read_auth_overrides()
                settings["reverse_proxy"] = {}
                write_private(SETTINGS_PATH, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
                audit("reverse_proxy_deleted", domain=domain)
                report(92, 4, "正在确认删除结果")
            with REVERSE_PROXY_JOBS_LOCK:
                REVERSE_PROXY_JOBS[job_id].update(
                    status="completed", progress=100, step=5, stage="反向代理删除完成",
                    detail="代理、配置和 TLS 文件已删除；域名保护规则已保留。",
                    result={"configured": False, "running": False, "domain": domain},
                    updated_at=time.time(),
                )
        except Exception as exc:
            with REVERSE_PROXY_JOBS_LOCK:
                REVERSE_PROXY_JOBS[job_id].update(status="failed", detail=str(exc), updated_at=time.time())

    threading.Thread(target=run, name=f"reverse-proxy-delete-{job_id[:8]}", daemon=True).start()
    return reverse_proxy_job_snapshot(job_id)


def delete_reverse_proxy_after_response() -> None:
    """Remove the proxy only after its own HTTP response has been delivered."""
    with REVERSE_PROXY_LOCK:
        try:
            saved = _read_auth_overrides().get("reverse_proxy", {})
            run_host_reverse_proxy_script(reverse_proxy_delete_script(str(saved.get("domain", ""))), "删除", timeout=45)
        except RuntimeError as exc:
            audit("reverse_proxy_delete_failed", detail=str(exc)[:500])
            return
    settings = _read_auth_overrides()
    settings["reverse_proxy"] = {}
    write_private(SETTINGS_PATH, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
    audit("reverse_proxy_deleted")


@PROXY_ROUTER.post("/api/reverse-proxy/delete")
def delete_reverse_proxy(background_tasks: BackgroundTasks) -> dict:
    # The browser may be connected through this proxy.  Deleting it before
    # writing the response would make the client misreport a successful
    # deletion as a failed request.
    background_tasks.add_task(delete_reverse_proxy_after_response)
    return {
        "configured": False,
        "running": False,
        "detail": "删除已确认；响应发送完成后将移除代理容器、配置和 TLS 文件。",
    }


@PROVIDERS_ROUTER.post("/api/providers")
def save_provider(provider: Provider) -> dict:
    try:
        result = PROVIDER_SERVICE.save(provider.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    audit("provider_saved", provider_id=provider.id)
    return result


@PROVIDERS_ROUTER.get("/api/providers/{provider_id}/auth")
def provider_auth_contents(provider_id: str, reveal: bool = False) -> dict:
    profile = read_profiles().get(provider_id)
    if not profile:
        raise HTTPException(404, "Provider profile not found")
    contents = profile.get("auth_contents", "")
    # Official-login tokens stay server-side. Show only redacted account and
    # session metadata so the edit view can confirm what is stored.
    if profile.get("auth_mode") == "chatgpt" and not reveal:
        contents = redacted_auth_snapshot(str(contents)) if contents else "官方登录认证尚未保存。"
    # Older pure-API profiles may have stored their key only in the profile.
    # The authenticated edit view uses this no-store endpoint to populate the
    # Key field, so preserve that behavior after upgrading those profiles.
    if profile.get("auth_mode") == "apikey" and not contents and profile.get("bearer_token"):
        contents = json.dumps({"OPENAI_API_KEY": profile["bearer_token"]}, indent=2)
    audit("provider_auth_revealed" if reveal else "provider_auth_viewed", provider_id=provider_id)
    return JSONResponse(
        {
            "auth_mode": profile.get("auth_mode", "apikey"),
            "contents": contents,
        },
        headers={"Cache-Control": "no-store"},
    )


@PROVIDERS_ROUTER.post("/api/providers/{provider_id}/activate")
def activate_provider(provider_id: str, verify: bool = True) -> dict:
    return switch_provider(provider_id, verify=verify)


def activation_job_snapshot(job_id: str) -> dict:
    with ACTIVATION_JOBS_LOCK:
        job = ACTIVATION_JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "切换任务不存在或已过期")
        return dict(job)


@PROVIDERS_ROUTER.post("/api/providers/{provider_id}/activate-progress")
def start_provider_activation(provider_id: str) -> dict:
    job_id = uuid.uuid4().hex
    with ACTIVATION_JOBS_LOCK:
        ACTIVATION_JOBS[job_id] = new_background_job("activation", {
            "id": job_id,
            "status": "running",
            "progress": 2,
            "stage": "正在准备切换任务",
            "detail": "",
            "result": None,
            "updated_at": time.time(),
        })

    def report(percent: int, stage: str) -> None:
        with ACTIVATION_JOBS_LOCK:
            job = ACTIVATION_JOBS.get(job_id)
            if job:
                job.update(progress=percent, stage=stage, updated_at=time.time())

    def run() -> None:
        try:
            result = switch_provider(provider_id, progress=report)
        except HTTPException as exc:
            with ACTIVATION_JOBS_LOCK:
                job = ACTIVATION_JOBS.get(job_id)
                if job:
                    job.update(
                        status="failed",
                        stage="切换未完成",
                        detail=activation_error_message(exc.detail),
                        updated_at=time.time(),
                    )
            return
        except Exception as exc:  # pragma: no cover - defensive boundary for background work
            with ACTIVATION_JOBS_LOCK:
                job = ACTIVATION_JOBS.get(job_id)
                if job:
                    job.update(status="failed", stage="切换未完成", detail=str(exc), updated_at=time.time())
            return
        with ACTIVATION_JOBS_LOCK:
            job = ACTIVATION_JOBS.get(job_id)
            if job:
                job.update(
                    status="completed",
                    progress=100,
                    stage="供应商切换完成",
                    detail=result["runtime"]["detail"],
                    result=result,
                    updated_at=time.time(),
                )

    threading.Thread(target=run, name=f"provider-activation-{job_id[:8]}", daemon=True).start()
    return activation_job_snapshot(job_id)


@PROVIDERS_ROUTER.get("/api/provider-activations/{job_id}")
def get_provider_activation(job_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(422, "无效的切换任务标识")
    return activation_job_snapshot(job_id)


@PROVIDERS_ROUTER.delete("/api/providers/{provider_id}")
def delete_provider(provider_id: str) -> dict:
    try:
        PROVIDER_SERVICE.delete(provider_id, active_provider_id=active_provider())
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    audit("provider_deleted", provider_id=provider_id)
    return {"deleted": provider_id}


@OPERATIONS_ROUTER.get("/api/preflight")
def get_preflight() -> dict:
    return preflight()


@OPERATIONS_ROUTER.get("/api/health")
def get_health() -> dict:
    return health_check()


@OPERATIONS_ROUTER.get("/api/health/plan")
def get_health_check_plan() -> dict:
    return {"checks": health_check_plan()}


def health_check_snapshot(job_id: str) -> dict:
    with HEALTH_CHECK_JOBS_LOCK:
        job = HEALTH_CHECK_JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "健康检查任务不存在或已过期")
        snapshot = dict(job)
        snapshot["checks"] = [dict(item) for item in job.get("checks", [])]
    return snapshot


@OPERATIONS_ROUTER.post("/api/health/progress")
def start_health_check() -> dict:
    job_id = uuid.uuid4().hex
    with HEALTH_CHECK_JOBS_LOCK:
        HEALTH_CHECK_JOBS[job_id] = new_background_job("health", {
            "id": job_id,
            "status": "running",
            "checks": [],
            "stage": "正在检查运行环境",
        })

    def report(item: dict[str, str]) -> None:
        with HEALTH_CHECK_JOBS_LOCK:
            job = HEALTH_CHECK_JOBS.get(job_id)
            if job:
                job["checks"].append(item)
                job["stage"] = f"已完成：{item['name']}"
                job.persist()

    def run() -> None:
        try:
            result = health_check(on_check=report)
        except Exception as exc:  # pragma: no cover - defensive background boundary
            with HEALTH_CHECK_JOBS_LOCK:
                job = HEALTH_CHECK_JOBS.get(job_id)
                if job:
                    job.update(status="failed", stage="健康检查失败", detail=str(exc))
            return
        with HEALTH_CHECK_JOBS_LOCK:
            job = HEALTH_CHECK_JOBS.get(job_id)
            if job:
                job.update(status="completed", stage="健康检查完成", result=result)

    threading.Thread(target=run, name=f"health-check-{job_id[:8]}", daemon=True).start()
    return health_check_snapshot(job_id)


@OPERATIONS_ROUTER.get("/api/health/progress/{job_id}")
def get_health_check(job_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(422, "无效的健康检查任务标识")
    return health_check_snapshot(job_id)


@OPERATIONS_ROUTER.post("/api/health/repair-config")
def repair_config_from_health() -> dict:
    if not CONFIG_PATH.exists():
        raise HTTPException(404, "未找到 config.toml")
    repaired = normalize_features_sections(config_text())
    try:
        tomllib.loads(repaired)
    except tomllib.TOMLDecodeError as exc:
        raise HTTPException(422, f"配置还包含无法自动修复的 TOML 错误：{exc}") from exc
    backup_id, _ = backup_state(reason="repair_config")
    write_private(CONFIG_PATH, repaired + "\n")
    audit("config_repaired", backup_id=backup_id)
    return {"detail": f"已合并重复的 [features] 配置段；已创建备份 {backup_id}。"}


@OPERATIONS_ROUTER.post("/api/health/install-codex")
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


@OPERATIONS_ROUTER.post("/api/health/update-codex")
def update_codex_from_health() -> dict:
    return install_codex_from_health()

@OPERATIONS_ROUTER.post("/api/health/deployment-key")
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


@OPERATIONS_ROUTER.get("/api/health/deployment-key/download")
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


@PROVIDERS_ROUTER.post("/api/providers/{provider_id}/capture-auth")
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
    backup_state(reason="auth_capture")
    profile["auth_contents"] = contents
    profiles[provider_id] = profile
    write_private(PROFILE_PATH, json.dumps({key: storage_profile(value) for key, value in profiles.items()}, ensure_ascii=False, indent=2) + "\n")
    audit("chatgpt_auth_captured", provider_id=provider_id)


@PROVIDERS_ROUTER.post("/api/providers/{provider_id}/official-login/start")
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
        DEVICE_LOGIN = DeviceLoginSession(provider_id, codex_home=CODEX_HOME, timeout_seconds=LOGIN_TIMEOUT_SECONDS)
        try:
            result = DEVICE_LOGIN.start()
        except RuntimeError as exc:
            DEVICE_LOGIN.status = "failed"
            DEVICE_LOGIN.detail = str(exc)[:300]
            raise HTTPException(503, DEVICE_LOGIN.detail) from exc
    audit("chatgpt_device_login_started", provider_id=provider_id)
    return result


@PROVIDERS_ROUTER.get("/api/official-login/status")
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


@PROVIDERS_ROUTER.post("/api/official-login/cancel")
def cancel_official_login() -> dict:
    with DEVICE_LOGIN_LOCK:
        if not DEVICE_LOGIN:
            return {"status": "idle", "detail": "没有正在进行的官方登录。"}
        DEVICE_LOGIN.cancel()
        audit("chatgpt_device_login_cancelled", provider_id=DEVICE_LOGIN.provider_id)
        return DEVICE_LOGIN.public_status()


@DIAGNOSTICS_ROUTER.post("/api/providers/{provider_id}/test")
def test_saved_provider(provider_id: str) -> dict:
    profile = read_profiles().get(provider_id)
    if not profile:
        raise HTTPException(404, "Provider profile not found")
    result = diagnose_profile(profile)
    audit("provider_tested", provider_id=provider_id, passed=result["ok"])
    return result


@DIAGNOSTICS_ROUTER.post("/api/providers/{provider_id}/relay-test")
def relay_test_provider(provider_id: str) -> dict:
    profile = read_profiles().get(provider_id)
    if not profile:
        raise HTTPException(404, "Provider profile not found")
    if active_provider() != provider_id:
        raise HTTPException(409, "Relay test requires the provider to be active")
    normalized = normalize_profile(profile)
    if normalized["mode"] != "pure_api":
        raise HTTPException(422, "Relay test is only available for pure API providers")
    model = str(normalized.get("model") or "").strip()
    if not model:
        raise HTTPException(422, "The active provider has no model configured")
    result = test_model_request_via_relay(normalized, model)
    audit("provider_relay_tested", provider_id=provider_id, passed=result["ok"])
    return result


@DIAGNOSTICS_ROUTER.post("/api/providers/diagnose")
def diagnose_unsaved_provider(request: ProviderDiagnosticRequest) -> dict:
    result = diagnose_profile(request.model_dump())
    audit("provider_diagnosed", passed=result["ok"])
    return result


def provider_diagnostic_snapshot(job_id: str) -> dict:
    with PROVIDER_DIAGNOSTIC_JOBS_LOCK:
        job = PROVIDER_DIAGNOSTIC_JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "供应商诊断任务不存在或已过期")
        snapshot = dict(job)
    snapshot["elapsed_ms"] = round((time.time() - snapshot.get("started_at", time.time())) * 1000)
    snapshot.pop("started_at", None)
    return snapshot


@DIAGNOSTICS_ROUTER.post("/api/providers/diagnose-progress")
def start_provider_diagnostic(request: ProviderDiagnosticRequest) -> dict:
    job_id = uuid.uuid4().hex
    with PROVIDER_DIAGNOSTIC_JOBS_LOCK:
        PROVIDER_DIAGNOSTIC_JOBS[job_id] = new_background_job("provider_diagnostic", {
            "id": job_id,
            "status": "running",
            "progress": 2,
            "stage": "正在准备供应商诊断",
            "result": None,
            "detail": "",
            "started_at": time.time(),
        })

    def report(percent: int, stage: str) -> None:
        with PROVIDER_DIAGNOSTIC_JOBS_LOCK:
            job = PROVIDER_DIAGNOSTIC_JOBS.get(job_id)
            if job:
                job.update(progress=percent, stage=stage)

    def run() -> None:
        try:
            result = diagnose_profile(request.model_dump(), progress=report)
        except Exception as exc:  # pragma: no cover - defensive background boundary
            with PROVIDER_DIAGNOSTIC_JOBS_LOCK:
                job = PROVIDER_DIAGNOSTIC_JOBS.get(job_id)
                if job:
                    job.update(status="failed", stage="供应商诊断失败", detail=str(exc))
            return
        with PROVIDER_DIAGNOSTIC_JOBS_LOCK:
            job = PROVIDER_DIAGNOSTIC_JOBS.get(job_id)
            if job:
                job.update(status="completed", progress=100, stage="供应商诊断完成", result=result)
        audit("provider_diagnosed", passed=result["ok"])

    threading.Thread(target=run, name=f"provider-diagnostic-{job_id[:8]}", daemon=True).start()
    return provider_diagnostic_snapshot(job_id)


@DIAGNOSTICS_ROUTER.get("/api/provider-diagnostics/{job_id}")
def get_provider_diagnostic(job_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(422, "无效的供应商诊断任务标识")
    return provider_diagnostic_snapshot(job_id)


def prune_model_diagnostic_jobs(now: float | None = None) -> None:
    """Bound the in-memory job registry without removing running jobs."""
    now = time.time() if now is None else now
    with MODEL_DIAGNOSTIC_JOBS_LOCK:
        for job_id, job in list(MODEL_DIAGNOSTIC_JOBS.items()):
            if job["status"] != "running" and now - job["updated_at"] > MODEL_DIAGNOSTIC_JOB_TTL_SECONDS:
                del MODEL_DIAGNOSTIC_JOBS[job_id]
        finished = sorted(
            ((job_id, job) for job_id, job in MODEL_DIAGNOSTIC_JOBS.items() if job["status"] != "running"),
            key=lambda pair: pair[1]["updated_at"],
        )
        for job_id, _ in finished[:max(0, len(MODEL_DIAGNOSTIC_JOBS) - MODEL_DIAGNOSTIC_MAX_JOBS)]:
            del MODEL_DIAGNOSTIC_JOBS[job_id]


def model_diagnostic_snapshot(job_id: str) -> dict:
    prune_model_diagnostic_jobs()
    with MODEL_DIAGNOSTIC_JOBS_LOCK:
        job = MODEL_DIAGNOSTIC_JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "模型诊断任务不存在或已过期")
        return dict(job)


def relay_diagnostic_eligible(profile: dict, names: list[str]) -> bool:
    """Only test the deployed Relay when the editor matches its active profile."""
    provider_id = str(profile.get("id") or "")
    saved = read_profiles().get(provider_id)
    if not saved or active_provider() != provider_id:
        return False
    saved = normalize_profile(saved)
    keys = ("base_url", "bearer_token", "protocol", "model")
    return (all(saved.get(key) == profile.get(key) for key in keys)
            and bool(saved.get("no_auth")) == bool(profile.get("no_auth"))
            and str(profile.get("model") or "") in names)


@DIAGNOSTICS_ROUTER.post("/api/providers/diagnose-models-progress")
def start_model_diagnostics(request: ModelDiagnosticRequest) -> dict:
    profile = normalize_profile(request.model_dump(exclude={"models"}))
    if profile["mode"] != "pure_api":
        raise HTTPException(422, "模型诊断仅适用于纯 API 供应商")
    usable, usability_detail = is_profile_usable(profile)
    if not usable:
        raise HTTPException(422, usability_detail or "请先填写有效的 Base URL 与 API Key")

    names: list[str] = []
    for entry in request.models:
        name = entry.name.strip()
        if name and name not in names:
            names.append(name)
    if not names:
        raise HTTPException(422, "请先从上游获取模型列表")
    if len(names) > MODEL_DIAGNOSTIC_MAX_MODELS:
        raise HTTPException(422, f"单次最多诊断 {MODEL_DIAGNOSTIC_MAX_MODELS} 个模型")

    prune_model_diagnostic_jobs()
    relay_eligible = relay_diagnostic_eligible(profile, names)
    job_id = uuid.uuid4().hex
    with MODEL_DIAGNOSTIC_JOBS_LOCK:
        if len(MODEL_DIAGNOSTIC_JOBS) >= MODEL_DIAGNOSTIC_MAX_JOBS:
            raise HTTPException(429, "正在运行的诊断任务过多，请稍后重试")
        MODEL_DIAGNOSTIC_JOBS[job_id] = new_background_job("model_diagnostic", {
            "id": job_id,
            "status": "running",
            "total": len(names),
            "completed": 0,
            "passed": 0,
            "failed": 0,
            "rate_limited": 0,
            "skipped": 0,
            "stage": "正在准备上游直连诊断",
            "results": [],
            "relay_result": {"status": "pending" if relay_eligible else "skipped",
                             "detail": "等待上游直连诊断" if relay_eligible else "仅当前启用、未修改的供应商选中模型可测试 Relay"},
            "cancel_requested": False,
            "updated_at": time.time(),
        })

    def skip_remaining(start_index: int, reason: str, kind: str) -> None:
        with MODEL_DIAGNOSTIC_JOBS_LOCK:
            job = MODEL_DIAGNOSTIC_JOBS[job_id]
            for name in names[start_index:]:
                job["results"].append({
                    "model": name, "status": "skipped", "http_status": None,
                    "elapsed_ms": 0, "detail": reason, "error_kind": kind, "retry_after": "",
                })
                job["skipped"] += 1
            job["completed"] = len(names)
            job["updated_at"] = time.time()

    def run() -> None:
        deadline = time.monotonic() + MODEL_DIAGNOSTIC_MAX_SECONDS
        final_status = "completed"
        for index, name in enumerate(names):
            with MODEL_DIAGNOSTIC_JOBS_LOCK:
                job = MODEL_DIAGNOSTIC_JOBS[job_id]
                cancelled = job["cancel_requested"]
                if not cancelled:
                    job.update(stage=f"上游直连（{index + 1}/{len(names)}）{name}", updated_at=time.time())
            if cancelled:
                final_status = "cancelled"
                skip_remaining(index, "用户取消，未发送请求", "cancelled")
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                final_status = "timed_out"
                skip_remaining(index, "达到诊断总时长上限，未发送请求", "time_budget_exceeded")
                break
            started_at = time.monotonic()
            try:
                result = test_model_request(
                    profile, name, timeout_seconds=max(1, min(20, int(remaining))), max_attempts=1
                )
            except Exception:  # pragma: no cover - defensive background boundary
                result = {"ok": False, "detail": "诊断请求出现内部错误", "error_kind": "diagnostic_error"}
            elapsed_ms = round((time.monotonic() - started_at) * 1000)
            passed = bool(result.get("ok"))
            detail = (
                f'上游直连成功：{result.get("detail") or "已生成输出"}'
                if passed else str(result.get("detail") or "请求失败")
            )
            result_kind = str(result.get("error_kind") or "")
            item = {
                "model": name,
                "endpoint": result.get("endpoint", ""),
                "status": "pass" if passed else "rate_limited" if result_kind == "rate_limited" else "fail",
                "http_status": result.get("status"),
                "elapsed_ms": elapsed_ms,
                "detail": detail[:600],
                "error_kind": result_kind,
                "retry_after": result.get("retry_after") or "",
            }
            with MODEL_DIAGNOSTIC_JOBS_LOCK:
                job = MODEL_DIAGNOSTIC_JOBS[job_id]
                job["results"].append(item)
                job["completed"] = index + 1
                job["passed"] += int(passed)
                job["failed"] += int(not passed and result_kind != "rate_limited")
                job["rate_limited"] += int(result_kind == "rate_limited")
                job["updated_at"] = time.time()
                cancelled = job["cancel_requested"]
            if result_kind == "rate_limited":
                final_status = "rate_limited"
                retry_hint = str(result.get("retry_after") or "").strip()
                skip_remaining(index + 1, f"上游已限流，未继续请求。请稍后{retry_hint + ' 秒后' if retry_hint.isdigit() else ''}重试。", "skipped_after_rate_limit")
                break
            if cancelled:
                final_status = "cancelled"
                skip_remaining(index + 1, "用户取消，未发送请求", "cancelled")
                break

        if final_status == "completed" and relay_eligible:
            selected = str(profile.get("model") or "")
            selected_passed = any(item["model"] == selected and item["status"] == "pass"
                                  for item in MODEL_DIAGNOSTIC_JOBS[job_id]["results"])
            if selected_passed and deadline - time.monotonic() > 1 and not MODEL_DIAGNOSTIC_JOBS[job_id]["cancel_requested"]:
                if PROVIDER_SWITCH_LOCK.acquire(blocking=False):
                    try:
                        if relay_diagnostic_eligible(profile, names):
                            with MODEL_DIAGNOSTIC_JOBS_LOCK:
                                MODEL_DIAGNOSTIC_JOBS[job_id]["stage"] = "正在测试 Relay 转换链路（模拟 Codex 请求）"
                            try:
                                relay = test_model_request_via_relay(
                                    profile, selected, timeout_seconds=max(1, min(20, int(deadline - time.monotonic())))
                                )
                            except Exception:  # pragma: no cover - defensive background boundary
                                relay = {"ok": False, "detail": "Relay 诊断出现内部错误"}
                            relay_result = {
                                "status": "pass" if relay.get("ok") else "fail",
                                "http_status": relay.get("status"),
                                "detail": (relay.get("detail") or "已生成可验证输出" if relay.get("ok") else relay.get("detail") or "请求失败")[:300],
                            }
                        else:
                            relay_result = {"status": "skipped", "detail": "活动供应商配置已变化，未测试 Relay"}
                    finally:
                        PROVIDER_SWITCH_LOCK.release()
                else:
                    relay_result = {"status": "skipped", "detail": "供应商正在切换，未测试 Relay"}
            else:
                relay_result = {"status": "skipped", "detail": "选中模型直连未通过、已取消或总时长不足"}
        elif relay_eligible:
            relay_result = {"status": "skipped", "detail": "批量诊断未完成，未测试 Relay"}
        else:
            relay_result = None

        with MODEL_DIAGNOSTIC_JOBS_LOCK:
            job = MODEL_DIAGNOSTIC_JOBS[job_id]
            if job["cancel_requested"] and final_status == "completed":
                final_status = "cancelled"
            if relay_result is not None:
                job["relay_result"] = relay_result
            stages = {
                "completed": "模型诊断完成",
                "rate_limited": "上游限流，已停止后续诊断",
                "cancelled": "模型诊断已取消",
                "timed_out": "达到诊断总时长上限",
            }
            job.update(status=final_status, stage=stages[final_status], updated_at=time.time())
            audit("provider_models_diagnosed", provider_id=profile.get("id") or None,
                  passed=str(job["passed"]), failed=str(job["failed"]))

    threading.Thread(target=run, name=f"model-diagnostic-{job_id[:8]}", daemon=True).start()
    return model_diagnostic_snapshot(job_id)


@DIAGNOSTICS_ROUTER.post("/api/model-diagnostics/{job_id}/cancel")
def cancel_model_diagnostics(job_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(422, "无效的模型诊断任务标识")
    with MODEL_DIAGNOSTIC_JOBS_LOCK:
        job = MODEL_DIAGNOSTIC_JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "模型诊断任务不存在或已过期")
        if job["status"] == "running":
            job["cancel_requested"] = True
            job["stage"] = "正在取消，等待当前请求结束"
            job["updated_at"] = time.time()
    return model_diagnostic_snapshot(job_id)


@DIAGNOSTICS_ROUTER.get("/api/model-diagnostics/{job_id}")
def get_model_diagnostics(job_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(422, "无效的模型诊断任务标识")
    return model_diagnostic_snapshot(job_id)


@DIAGNOSTICS_ROUTER.post("/api/upstream/models")
def fetch_models_from_upstream(request: UpstreamModelFetch) -> dict:
    return fetch_upstream_models(request)


SESSION_SERVICE = SessionService(
    valid_id=lambda value: bool(THREAD_ID.fullmatch(value)),
    list_active=list_server_sessions, list_archived=list_archived_server_sessions,
    start_app_server=lambda: run_host_app_server_control("start"),
    archive_remote=run_host_session_archive, delete_remote=run_host_session_delete,
    confirm_archive=confirm_session_archive_in_background,
    migrate=lambda source, apply, backup: migrate_session_provider(SESSION_PROVIDER_ID, source, apply=apply, backup_dir=backup),
    backup=backup_state, profile_exists=lambda provider_id: provider_id in read_profiles(), audit=audit,
)


def session_result(operation):
    try:
        return operation()
    except SessionError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@SESSIONS_ROUTER.get("/api/sessions")
def get_server_sessions() -> dict:
    return SESSION_SERVICE.active()


@SESSIONS_ROUTER.get("/api/archived-sessions")
def get_archived_server_sessions() -> dict:
    return SESSION_SERVICE.archived()


@SESSIONS_ROUTER.post("/api/sessions/{thread_id}/archive")
def archive_server_session(thread_id: str) -> dict:
    with SESSION_MUTATION_LOCK:
        return session_result(lambda: SESSION_SERVICE.archive(thread_id, True))


@SESSIONS_ROUTER.post("/api/archived-sessions/{thread_id}/unarchive")
def unarchive_server_session(thread_id: str) -> dict:
    with SESSION_MUTATION_LOCK:
        return session_result(lambda: SESSION_SERVICE.archive(thread_id, False))


@SESSIONS_ROUTER.post("/api/sessions/migrate")
def migrate_server_sessions(request: SessionMigrationRequest) -> dict:
    return session_result(lambda: SESSION_SERVICE.migrate_provider(request.target_provider, request.source_provider, request.apply, SESSION_PROVIDER_ID))


@SESSIONS_ROUTER.delete("/api/sessions/{thread_id}")
def delete_server_session(thread_id: str) -> dict:
    with SESSION_MUTATION_LOCK:
        return session_result(lambda: SESSION_SERVICE.delete(thread_id))


@BACKUPS_ROUTER.get("/api/backups")
def list_backups() -> list[dict]:
    removed = prune_backup_history()
    if removed:
        audit("backup_history_pruned", removed=",".join(removed))
    return BACKUP_SERVICE.list_backups()


@BACKUPS_ROUTER.post("/api/backups/{backup_id}/restore")
def restore_backup(backup_id: str) -> dict:
    source = backup_directory(backup_id)
    current_id, _ = backup_state(include_sessions=True, reason="before_restore")
    try:
        restored = restore_backup_state(source)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(500, f"恢复备份失败，当前状态已保存为 {current_id}") from exc
    audit("backup_restore_completed", backup_id=backup_id, before_backup_id=current_id)
    return {"restored": backup_id, "before_restore_backup": current_id, "files": restored, "detail": "备份已恢复；恢复前的当前状态也已自动备份。"}


app.include_router(SESSIONS_ROUTER)
app.include_router(BACKUPS_ROUTER)
app.include_router(PROXY_ROUTER)
app.include_router(OPERATIONS_ROUTER)
app.include_router(DIAGNOSTICS_ROUTER)
app.include_router(PROVIDERS_ROUTER)
app.include_router(ACCOUNT_ROUTER)

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return render_index()
