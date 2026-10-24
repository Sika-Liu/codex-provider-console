import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class AccountSettingsTests(unittest.TestCase):
    def test_account_api_remains_available_without_settings_page(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('@ACCOUNT_ROUTER.get("/api/account")', source)
        self.assertIn('@ACCOUNT_ROUTER.post("/api/account")', source)
        self.assertIn("panel_password_hash", source)
        page = (ROOT / "web" / "pages.py").read_text(encoding="utf-8")
        self.assertNotIn('id="console-settings"', page)
        self.assertNotIn("saveAccountSettings", page)
        self.assertIn('id="account-menu"', page)
        self.assertIn("loadAccountMenu", page)
        self.assertIn("account-password-eye", page)
        self.assertIn("account-password-confirm", page)
        self.assertIn("account-password-mask", page)
        self.assertNotIn("event.target.id==='account-password-mask'", page)

    def test_settings_writes_preserve_auth_overrides(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("settings = _read_auth_overrides()"), 4)


if __name__ == "__main__":
    unittest.main()
