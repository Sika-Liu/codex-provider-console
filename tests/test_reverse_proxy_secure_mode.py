import unittest
from pathlib import Path


class ReverseProxySecureModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (Path(__file__).parents[1] / "app.py").read_text(encoding="utf-8")

    def test_endpoint_requires_https_proxy_identity(self):
        self.assertIn("/api/reverse-proxy/enable-secure-mode", self.source)
        self.assertIn("x-forwarded-proto", self.source)
        self.assertIn("request_host != domain", self.source)
        self.assertIn("https_verified", self.source)
        self.assertIn("https_error", self.source)

    def test_secure_mode_updates_host_environment_and_recreates_panel(self):
        self.assertIn('PANEL_BIND", "127.0.0.1"', self.source)
        self.assertIn('PANEL_COOKIE_SECURE", "true"', self.source)
        self.assertIn("--force-recreate codex-provider-console", self.source)

    def test_ui_exposes_explicit_secure_mode_action(self):
        self.assertIn('id="proxy-enable-secure"', self.source)
        self.assertIn("enableProxySecureMode", self.source)
        self.assertIn("result.secure_mode", self.source)


if __name__ == "__main__":
    unittest.main()
