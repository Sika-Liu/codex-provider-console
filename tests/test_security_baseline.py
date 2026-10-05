import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SecurityBaselineTests(unittest.TestCase):
    def test_installer_defaults_to_loopback_binding(self):
        source = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn('PANEL_BIND="127.0.0.1"', source)
        self.assertIn("The default binds the panel to 127.0.0.1", source)

    def test_compose_passes_bind_to_the_panel(self):
        source = (ROOT / "compose.yml").read_text(encoding="utf-8")
        self.assertIn('PANEL_BIND: "${PANEL_BIND:-127.0.0.1}"', source)
        self.assertIn('"127.0.0.1:${RELAY_PORT:-57321}:57321"', source)

    def test_login_has_a_bounded_failure_window(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("LOGIN_FAILURE_WINDOW_SECONDS = 5 * 60", source)
        self.assertIn("login_is_rate_limited", source)
        self.assertIn("raise HTTPException(429", source)
        self.assertIn("clear_failed_logins(client_key)", source)

    def test_security_warnings_are_returned_without_provider_banner(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('"security_warnings": security_warnings', source)
        self.assertNotIn("renderSecurityWarnings(d.preflight)", source)
        self.assertIn("PANEL_COOKIE_SECURE=true", source)


if __name__ == "__main__":
    unittest.main()
