"""Password-recovery and recovery-email workflows without HTTP dependencies."""
from __future__ import annotations
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

class RecoveryError(ValueError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status

@dataclass
class RecoveryService:
    read_settings: Callable[[], dict]
    write_settings: Callable[[dict], None]
    session_secret: Callable[[], str]
    password_matches: Callable[[str], bool]
    password_reset: Callable[[str], None]
    send_email: Callable[[str, str, str], None]
    email_ready: Callable[[], bool]
    audit: Callable[..., None]
    code_lifetime: int = 600
    grant_lifetime: int = 300
    _pending: dict[str, dict] = field(default_factory=dict, init=False, repr=False)
    _send_events: dict[str, list[float]] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @staticmethod
    def mask(email: str) -> str:
        if not email or "@" not in email:
            return ""
        name, domain = email.rsplit("@", 1)
        return name[:1] + "***@" + domain

    @staticmethod
    def _digest(code: str, salt: str) -> str:
        return hashlib.sha256(f"{salt}:{code}".encode("utf-8")).hexdigest()

    def _new_code(self) -> tuple[str, dict]:
        code = f"{secrets.randbelow(100_000_000):08d}"
        salt = secrets.token_urlsafe(24)
        return code, {"salt": salt, "digest": self._digest(code, salt), "expires": time.time() + self.code_lifetime, "attempts": 0}

    def _send_allowed(self, action: str, client_key: str) -> bool:
        now = time.monotonic()
        for key, limit in ((f"{action}:global", 5), (f"{action}:client:{client_key}", 3)):
            recent = [sent for sent in self._send_events.get(key, []) if now - sent < 15 * 60]
            self._send_events[key] = recent
            if len(recent) >= limit:
                return False
        for key in (f"{action}:global", f"{action}:client:{client_key}"):
            self._send_events[key].append(now)
        return True

    def status(self) -> dict:
        email = str(self.read_settings().get("panel_recovery_email") or "")
        return {"configured": self.email_ready(), "email": self.mask(email), "bound": bool(email)}

    def _send_code(self, action: str, email: str, client_key: str, subject: str, body: str) -> dict:
        if not self.email_ready():
            raise RecoveryError(503, "邮件服务尚未配置")
        with self._lock:
            if not self._send_allowed(action, client_key):
                raise RecoveryError(429, "验证码发送过于频繁，请稍后重试")
            code, pending = self._new_code()
            pending.update({"email": email, "session_secret": self.session_secret()})
            self._pending[action] = pending
        try:
            self.send_email(email, subject, body.format(code=code))
        except Exception:
            with self._lock:
                if self._pending.get(action) is pending:
                    self._pending.pop(action, None)
            raise
        return {"sent": True, "expires_in": self.code_lifetime}

    def request_binding(self, email: str, client_key: str) -> dict:
        email = email.strip().lower()
        if email == self.read_settings().get("panel_recovery_email"):
            raise RecoveryError(400, "该邮箱已经绑定")
        return self._send_code("bind", email, client_key, "验证 Codex 控制台辅助邮箱", "邮箱绑定验证码：{code}。10 分钟内有效；如非本人操作，请忽略。")

    def request_unbind(self, client_key: str) -> dict:
        email = str(self.read_settings().get("panel_recovery_email") or "")
        if not email:
            raise RecoveryError(400, "尚未绑定辅助邮箱")
        result = self._send_code("unbind", email, client_key, "解除 Codex 控制台辅助邮箱绑定", "邮箱解绑验证码：{code}。10 分钟内有效；如非本人操作，请忽略。")
        result["email"] = self.mask(email)
        return result

    def _verify(self, action: str, code: str, *, email: str | None = None) -> dict:
        pending = self._pending.get(action)
        expected_email = email if email is not None else str(self.read_settings().get("panel_recovery_email") or "")
        if not pending or pending["expires"] < time.time() or pending["session_secret"] != self.session_secret() or pending["email"] != expected_email:
            self._pending.pop(action, None)
            raise RecoveryError(400, "验证码无效或已过期")
        pending["attempts"] += 1
        if pending["attempts"] > 5:
            self._pending.pop(action, None)
            raise RecoveryError(400, "验证码尝试次数过多，请重新发送")
        if not hmac.compare_digest(self._digest(code, pending["salt"]), pending["digest"]):
            raise RecoveryError(400, "验证码不正确")
        return pending

    def verify_binding(self, code: str) -> dict:
        with self._lock:
            pending = self._verify("bind", code, email=None)
            email = pending["email"]
            settings = self.read_settings()
            old_email = str(settings.get("panel_recovery_email") or "")
            settings["panel_recovery_email"] = email
            self.write_settings(settings)
            self._pending.clear()
        if old_email and old_email != email:
            try:
                self.send_email(old_email, "Codex 控制台辅助邮箱已更换", "控制台辅助邮箱已更换。如非本人操作，请通过服务器 SSH 立即重置管理员密码。")
            except Exception:
                self.audit("recovery_email_change_notice_failed")
        self.audit("recovery_email_bound")
        return {"bound": True, "email": self.mask(email)}

    def unbind(self, code: str) -> dict:
        with self._lock:
            self._verify("unbind", code)
            settings = self.read_settings()
            if not settings.get("panel_recovery_email"):
                raise RecoveryError(400, "尚未绑定辅助邮箱")
            settings.pop("panel_recovery_email", None)
            self.write_settings(settings)
            self._pending.clear()
        self.audit("recovery_email_unbound")
        return {"bound": False}

    def request_reset(self, email: str, client_key: str) -> dict:
        bound = str(self.read_settings().get("panel_recovery_email") or "")
        if not bound:
            raise RecoveryError(400, "未添加辅助邮箱，请通过服务器 SSH 重置密码。")
        if not hmac.compare_digest(email.strip().lower(), bound.lower()):
            raise RecoveryError(400, "辅助邮箱不匹配")
        result = self._send_code("reset", bound, client_key, "重置 Codex 控制台密码", "密码重置验证码：{code}。10 分钟内有效；如非本人操作，请忽略。")
        result["message"] = "验证码已发送至辅助邮箱。"
        return result

    def verify_reset(self, code: str) -> dict:
        with self._lock:
            pending = self._verify("reset", code)
            token = secrets.token_urlsafe(32)
            self._pending["grant"] = {"digest": hashlib.sha256(token.encode("utf-8")).hexdigest(), "expires": time.time() + self.grant_lifetime, "session_secret": self.session_secret(), "email": pending["email"]}
            self._pending.pop("reset", None)
        return {"reset_token": token, "expires_in": self.grant_lifetime}

    def complete_reset(self, token: str, new_password: str) -> dict:
        with self._lock:
            grant = self._pending.get("grant")
            email = str(self.read_settings().get("panel_recovery_email") or "")
            digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
            if not grant or grant["expires"] < time.time() or grant["session_secret"] != self.session_secret() or grant["email"] != email or not hmac.compare_digest(digest, grant["digest"]):
                raise RecoveryError(400, "重置凭证无效或已过期")
            if self.password_matches(new_password):
                raise RecoveryError(400, "新密码不能与当前密码相同")
            self.password_reset(new_password)
            self._pending.clear()
        self.audit("account_password_reset")
        return {"reset": True, "sessions_invalidated": True}
