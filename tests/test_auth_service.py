import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path

from security.auth_service import AuthService


class AuthServiceTests(unittest.TestCase):
    def make_service(self, root: Path, **kwargs) -> AuthService:
        return AuthService(
            username=kwargs.get("username", "admin"),
            password=kwargs.get("password", "plain-password"),
            session_secret=kwargs.get("session_secret", "session-secret"),
            settings_path=root / "settings.json",
            session_max_age=kwargs.get("session_max_age", 60),
            failure_window_seconds=kwargs.get("failure_window_seconds", 300),
            max_failures=kwargs.get("max_failures", 2),
        )

    def test_session_round_trip_and_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(Path(directory))
            token = service.session_token("admin")
            self.assertTrue(service.valid_session(token))
            self.assertFalse(service.valid_session(service.session_token("other")))
            service.session_max_age = -1
            self.assertFalse(service.valid_session(token))

    def test_password_hash_override_is_used_without_exposing_plaintext(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            salt = "test-salt"
            digest = hashlib.pbkdf2_hmac("sha256", b"new-password", salt.encode(), 210_000).hex()
            (root / "settings.json").write_text(
                json.dumps({"panel_password_hash": digest, "panel_password_salt": salt}),
                encoding="utf-8",
            )
            service = self.make_service(root)
            self.assertTrue(service.current_password_matches("new-password"))
            self.assertFalse(service.current_password_matches("plain-password"))

    def test_runtime_account_update_invalidates_old_sessions_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(Path(directory))
            old_token = service.session_token("admin")
            (Path(directory) / "settings.json").write_text(json.dumps({"panel_password_hash": "persisted-hash"}), encoding="utf-8")
            service.update_runtime(username="new-admin", password="", session_secret="new-secret")
            self.assertFalse(service.valid_session(old_token))
            self.assertFalse(service.current_password_matches("plain-password"))
            self.assertTrue(service.valid_session(service.session_token("new-admin")))

    def test_login_rate_limit_window_and_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.make_service(Path(directory), max_failures=2)
            now = time.monotonic()
            service.record_failed_login("client", now)
            service.record_failed_login("client", now)
            self.assertTrue(service.login_is_rate_limited("client", now))
            service.clear_failed_logins("client")
            self.assertFalse(service.login_is_rate_limited("client", now))


if __name__ == "__main__":
    unittest.main()
