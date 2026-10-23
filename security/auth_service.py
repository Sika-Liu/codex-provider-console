"""Authentication service separated from HTTP route and application wiring."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock


@dataclass
class AuthService:
    """Own panel password, session-token, and login-rate-limit behavior.

    The service deliberately has no FastAPI dependency. Routes can adapt request
    objects to the small string inputs used here, which keeps the security logic
    directly unit-testable.
    """

    username: str
    password: str
    session_secret: str
    settings_path: Path
    session_max_age: int = 12 * 60 * 60
    failure_window_seconds: int = 5 * 60
    max_failures: int = 10
    failures_lock: Lock = field(default_factory=Lock, repr=False)
    failures: dict[str, list[float]] = field(default_factory=dict, repr=False)

    def _read_overrides(self) -> dict:
        try:
            raw = json.loads(self.settings_path.read_text(encoding="utf-8")) if self.settings_path.exists() else {}
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def current_password_matches(self, password: str) -> bool:
        overrides = self._read_overrides()
        stored_hash = str(overrides.get("panel_password_hash") or "")
        if stored_hash:
            salt = str(overrides.get("panel_password_salt") or "")
            candidate = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), salt.encode("utf-8"), 210_000
            ).hex()
            return hmac.compare_digest(candidate, stored_hash)
        return bool(self.password) and hmac.compare_digest(password, self.password)

    def auth_configured(self) -> bool:
        overrides = self._read_overrides()
        return bool(self.username and (self.password or overrides.get("panel_password_hash")) and self.session_secret)

    def session_token(self, username: str) -> str:
        payload = f"{int(time.time())}:{username}".encode("utf-8")
        signature = hmac.new(self.session_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest().encode("ascii")
        return base64.urlsafe_b64encode(payload + b":" + signature).decode("ascii").rstrip("=")

    def valid_session(self, token: str | None) -> bool:
        if not token or not self.auth_configured():
            return False
        try:
            padded = token + "=" * (-len(token) % 4)
            payload, signature = base64.urlsafe_b64decode(padded.encode("ascii")).rsplit(b":", 1)
            expected = hmac.new(self.session_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest().encode("ascii")
            timestamp, username = payload.decode("utf-8").split(":", 1)
            return (
                hmac.compare_digest(signature, expected)
                and hmac.compare_digest(username, self.username)
                and int(timestamp) + self.session_max_age >= int(time.time())
            )
        except (ValueError, UnicodeDecodeError):
            return False

    @staticmethod
    def client_key(client_host: str | None) -> str:
        return client_host or "unknown"

    def login_is_rate_limited(self, client_key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self.failures_lock:
            failures = [
                occurred
                for occurred in self.failures.get(client_key, [])
                if now - occurred < self.failure_window_seconds
            ]
            if failures:
                self.failures[client_key] = failures
            else:
                self.failures.pop(client_key, None)
            return len(failures) >= self.max_failures

    def record_failed_login(self, client_key: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self.failures_lock:
            failures = [
                occurred
                for occurred in self.failures.get(client_key, [])
                if now - occurred < self.failure_window_seconds
            ]
            failures.append(now)
            self.failures[client_key] = failures

    def clear_failed_logins(self, client_key: str) -> None:
        with self.failures_lock:
            self.failures.pop(client_key, None)
