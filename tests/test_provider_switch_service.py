import tempfile
import threading
import unittest
from pathlib import Path

from fastapi import HTTPException

from application.provider_switch_service import ProviderSwitchDependencies, ProviderSwitchService


class ProviderSwitchServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.audits: list[tuple[str, dict]] = []
        self.profile = {
            "id": "example",
            "name": "Example",
            "mode": "pure_api",
            "protocol": "responses",
            "base_url": "https://provider.example",
            "bearer_token": "secret",
            "models": [{"name": "gpt-test"}],
            "model": "gpt-test",
        }

    def tearDown(self):
        self.temp.cleanup()

    def service(self, *, switching_enabled=True, diagnostic=None):
        diagnostic = diagnostic or {"ok": True, "checks": [], "summary": "通过"}
        unexpected = lambda *args, **kwargs: self.fail("unexpected side effect")
        dependencies = ProviderSwitchDependencies(
            panel_settings=lambda: {"provider_switching_enabled": switching_enabled},
            read_profiles=lambda: {"example": dict(self.profile)},
            active_provider=lambda: None,
            active_default_model=lambda: None,
            diagnose_profile=lambda profile: diagnostic,
            backup_state=unexpected,
            config_text=lambda: "",
            run_host_app_server_control=unexpected,
            migrate_session_provider=unexpected,
            read_auth_overrides=lambda: {},
            write_private=unexpected,
            restore_session_state=unexpected,
            audit=lambda action, **details: self.audits.append((action, details)),
            prune_provider_switch_backups=lambda: [],
            app_server_lock=threading.Lock(),
            common_config_path=self.root / "common.toml",
            config_path=self.root / "config.toml",
            auth_path=self.root / "auth.json",
            profile_path=self.root / "profiles.json",
            settings_path=self.root / "settings.json",
            session_provider_id="custom",
            host_relay_base_url="http://127.0.0.1:57321/v1",
        )
        return ProviderSwitchService(dependencies)

    def test_disabled_switch_rejects_before_any_side_effect(self):
        with self.assertRaises(HTTPException) as caught:
            self.service(switching_enabled=False).run("example")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.audits, [])

    def test_failed_diagnostic_audits_and_preserves_current_configuration(self):
        diagnostic = {
            "ok": False,
            "summary": "连接失败",
            "checks": [{"name": "供应商", "status": "fail", "detail": "连接失败"}],
        }
        with self.assertRaises(HTTPException) as caught:
            self.service(diagnostic=diagnostic).run("example")
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(self.audits[0][0], "provider_switch_rejected")
        self.assertIn("连接失败", self.audits[0][1]["detail"])
        self.assertFalse((self.root / "config.toml").exists())


if __name__ == "__main__":
    unittest.main()
