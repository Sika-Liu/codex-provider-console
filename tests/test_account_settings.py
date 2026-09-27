import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class AccountSettingsTests(unittest.TestCase):
    def test_account_api_and_settings_ui_exist(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/api/account")', source)
        self.assertIn('@app.post("/api/account")', source)
        self.assertIn("panel_password_hash", source)
        self.assertIn("系统设置", source)
        self.assertIn("saveAccountSettings", source)

    def test_settings_writes_preserve_auth_overrides(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("settings = _read_auth_overrides()"), 4)


if __name__ == "__main__":
    unittest.main()
