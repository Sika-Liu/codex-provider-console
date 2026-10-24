import unittest
from pathlib import Path


class ReverseProxySecureModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).parents[1]
        cls.source = (root / "app.py").read_text(encoding="utf-8")
        cls.gateway = (root / "infrastructure" / "reverse_proxy_gateway.py").read_text(encoding="utf-8")

    def test_endpoint_requires_https_proxy_identity(self):
        self.assertIn("/api/reverse-proxy/enable-secure-mode", self.source)
        self.assertIn("/api/reverse-proxy/disable-secure-mode", self.source)
        self.assertIn("x-forwarded-proto", self.source)
        self.assertIn("request_host != domain", self.source)
        self.assertIn("https_verified", self.source)
        self.assertIn("https_error", self.source)

    def test_secure_mode_updates_host_environment_and_recreates_panel(self):
        self.assertIn('PANEL_BIND", "127.0.0.1"', self.gateway)
        self.assertIn('PANEL_COOKIE_SECURE", "true"', self.gateway)
        self.assertIn('settings["panel_cookie_secure"] = True', self.gateway)
        self.assertIn('settings["panel_cookie_secure"] = False', self.gateway)
        self.assertIn('"docker", "compose", "up"', self.gateway)
        self.assertNotIn("sleep 8; exec docker compose", self.gateway)
        self.assertIn("import json, os, re, shutil, subprocess, uuid", self.gateway)

    def test_ui_exposes_explicit_secure_mode_action(self):
        self.assertIn('id="proxy-enable-secure"', self.source)
        self.assertIn("enableProxySecureMode", self.source)
        self.assertIn("result.secure_mode", self.source)
        self.assertIn('id="proxy-disable-secure"', self.source)
        self.assertIn("disableProxySecureMode", self.source)
        self.assertNotIn("renderSecurityWarnings(d.preflight)", self.source)
        self.assertIn("确认启用安全模式", self.source)
        self.assertIn("安全性更高", self.source)
        self.assertNotIn('id="proxy-security-status"', self.source)


if __name__ == "__main__":
    unittest.main()
