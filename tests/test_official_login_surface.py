import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class OfficialLoginSurfaceTests(unittest.TestCase):
    def test_official_login_uses_the_device_code_flow(self):
        routes = (ROOT / "app.py").read_text(encoding="utf-8")
        session = (ROOT / "application" / "official_login.py").read_text(encoding="utf-8")
        self.assertIn('account/login/start", {"type": "chatgptDeviceCode"}', session)
        self.assertIn('@PROVIDERS_ROUTER.get("/api/official-login/status")', routes)
        self.assertIn('@PROVIDERS_ROUTER.post("/api/official-login/cancel")', routes)

    def test_official_auth_snapshot_is_not_returned_to_the_browser(self):
        page = (pathlib.Path(__file__).resolve().parents[1] / "web" / "pages.py").read_text(encoding="utf-8")
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('if profile.get("auth_mode") == "chatgpt":\n        contents = ""', source)
        self.assertIn("auth_contents:auth_mode==='chatgpt'?'':$('#auth-preview').value", page)
        self.assertIn('令牌不会在页面显示', page)
        self.assertNotIn('认证令牌已显示在 auth.json 区域', page)


if __name__ == "__main__":
    unittest.main()
