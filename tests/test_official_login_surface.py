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

    def test_official_auth_snapshot_is_redacted_before_display(self):
        page = (pathlib.Path(__file__).resolve().parents[1] / "web" / "pages.py").read_text(encoding="utf-8")
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('redacted_auth_snapshot', source)
        self.assertIn("data.contents||'官方登录认证尚未保存。'", page)
        self.assertIn("auth_contents:auth_mode==='chatgpt'?'':$('#auth-preview').value", page)
        self.assertIn('id=\"auth-visibility-toggle\"', page)
        self.assertIn('toggleAuthVisibility()', page)


if __name__ == "__main__":
    unittest.main()
