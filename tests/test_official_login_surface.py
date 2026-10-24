import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class OfficialLoginSurfaceTests(unittest.TestCase):
    def test_official_login_uses_the_device_code_flow(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('account/login/start", {"type": "chatgptDeviceCode"}', source)
        self.assertIn('@app.get("/api/official-login/status")', source)
        self.assertIn('@app.post("/api/official-login/cancel")', source)

    def test_official_auth_snapshot_is_not_returned_to_the_browser(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('if profile.get("auth_mode") == "chatgpt":\n        contents = ""', source)
        self.assertIn("auth_contents:auth_mode==='chatgpt'?'':$('#auth-preview').value", source)
        self.assertIn('令牌不会在页面显示', source)
        self.assertNotIn('认证令牌已显示在 auth.json 区域', source)


if __name__ == "__main__":
    unittest.main()
