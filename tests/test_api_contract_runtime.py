import unittest
from unittest.mock import patch

try:
    import app
    from fastapi.testclient import TestClient
except ImportError:
    app = None
    TestClient = None


@unittest.skipIf(app is None, "FastAPI runtime is unavailable on the host")
class ApiContractRuntimeTests(unittest.TestCase):
    def test_login_and_logout_contract_when_auth_is_disabled(self):
        with patch.object(app, "AUTH_ENABLED", False):
            with TestClient(app.app) as client:
                login = client.post("/login", json={"username": "ignored", "password": "ignored"})
                self.assertEqual(login.status_code, 200)
                self.assertEqual(login.json(), {"authenticated": True})
                logout = client.post("/logout")
        self.assertEqual(logout.status_code, 200)
        self.assertEqual(logout.json(), {"authenticated": False})

    def test_protected_api_requires_a_valid_session(self):
        with patch.object(app, "AUTH_ENABLED", True), patch.object(app, "auth_configured", return_value=True):
            with TestClient(app.app) as client:
                response = client.get("/api/status")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "Authentication required"})


if __name__ == "__main__":
    unittest.main()
