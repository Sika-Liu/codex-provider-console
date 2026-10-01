import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class AccountSettingsTests(unittest.TestCase):
    def test_account_api_remains_available_without_settings_page(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/api/account")', source)
        self.assertIn('@app.post("/api/account")', source)
        self.assertIn("panel_password_hash", source)
        self.assertNotIn('id="console-settings"', source)
        self.assertNotIn("saveAccountSettings", source)
        self.assertIn("account-menu-toggle", source)
        self.assertIn("account-change-password", source)
        self.assertIn("account-password-eye", source)
        self.assertIn("account-password-confirm", source)

    def test_settings_writes_preserve_auth_overrides(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("settings = _read_auth_overrides()"), 4)


if __name__ == "__main__":
    unittest.main()
