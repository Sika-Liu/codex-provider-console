"""Local TOTP setup and password recovery without an external delivery service."""
from __future__ import annotations

import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import pyotp
import qrcode
from qrcode.image.svg import SvgPathImage


class TOTPError(ValueError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class TOTPService:
    read_settings: Callable[[], dict]
    write_settings: Callable[[dict], None]
    session_secret: Callable[[], str]
    password_matches: Callable[[str], bool]
    password_reset: Callable[[str], None]
    audit: Callable[..., None]
    issuer: str = "Codex 控制台"
    code_lifetime: int = 300
    _pending_secret: str | None = field(default=None, init=False, repr=False)
    _pending_expires: float = field(default=0, init=False, repr=False)
    _reset_grant: dict | None = field(default=None, init=False, repr=False)
    _attempts: dict[str, list[float]] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def status(self) -> dict:
        return {"configured": bool(self.read_settings().get("panel_totp_secret"))}

    def setup(self, account_name: str) -> dict:
        with self._lock:
            if self.read_settings().get("panel_totp_secret"):
                raise TOTPError(409, "验证器已经绑定")
            self._pending_secret = pyotp.random_base32()
            self._pending_expires = time.time() + self.code_lifetime
            uri = pyotp.TOTP(self._pending_secret).provisioning_uri(
                name=account_name, issuer_name=self.issuer
            )
            qr = qrcode.make(uri, image_factory=SvgPathImage)
            qr_svg = qr.to_string().decode("utf-8")
            return {
                "account_name": account_name,
                "issuer": self.issuer,
                "secret": self._pending_secret,
                "provisioning_uri": uri,
                "qr_svg": qr_svg,
                "expires_in": self.code_lifetime,
            }

    def confirm(self, code: str) -> dict:
        with self._lock:
            if not self._pending_secret or self._pending_expires < time.time():
                self._pending_secret = None
                raise TOTPError(400, "验证器绑定已过期，请重新开始")
            if not re.fullmatch(r"\d{6}", code) or not pyotp.TOTP(self._pending_secret).verify(code, valid_window=1):
                raise TOTPError(400, "验证器验证码不正确")
            settings = self.read_settings()
            settings["panel_totp_secret"] = self._pending_secret
            self.write_settings(settings)
            self._pending_secret = None
            self._pending_expires = 0
        self.audit("totp_bound")
        return {"configured": True}

    def disable(self, code: str) -> dict:
        with self._lock:
            secret = str(self.read_settings().get("panel_totp_secret") or "")
            if not secret or not pyotp.TOTP(secret).verify(code, valid_window=1):
                raise TOTPError(400, "验证器验证码不正确")
            settings = self.read_settings()
            settings.pop("panel_totp_secret", None)
            self.write_settings(settings)
            self._reset_grant = None
        self.audit("totp_unbound")
        return {"configured": False}

    def recover(self, expected_username: str, code: str, client_key: str) -> dict:
        now = time.monotonic()
        with self._lock:
            recent = [stamp for stamp in self._attempts.get(client_key, []) if now - stamp < 15 * 60]
            if len(recent) >= 5:
                raise TOTPError(429, "验证尝试过多，请 15 分钟后重试")
            recent.append(now)
            self._attempts[client_key] = recent
            secret = str(self.read_settings().get("panel_totp_secret") or "")
            if not expected_username or not secret or not re.fullmatch(r"\d{6}", code) or not pyotp.TOTP(secret).verify(code, valid_window=1):
                raise TOTPError(400, "验证器验证码不正确")
            token = secrets.token_urlsafe(32)
            self._reset_grant = {
                "token": token,
                "expires": time.time() + self.code_lifetime,
                "session_secret": self.session_secret(),
            }
            return {"reset_token": token, "expires_in": self.code_lifetime}

    def complete_reset(self, token: str, new_password: str) -> dict:
        with self._lock:
            grant = self._reset_grant
            if not grant or grant["expires"] < time.time() or grant["session_secret"] != self.session_secret() or not secrets.compare_digest(token, grant["token"]):
                raise TOTPError(400, "重置凭证无效或已过期")
            if self.password_matches(new_password):
                raise TOTPError(400, "新密码不能与当前密码相同")
            self.password_reset(new_password)
            self._reset_grant = None
        self.audit("totp_password_reset")
        return {"reset": True, "sessions_invalidated": True}


def hmac_compare(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)
