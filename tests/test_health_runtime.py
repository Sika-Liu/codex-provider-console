import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

try:
    import app
except ImportError:
    app = None


@unittest.skipIf(app is None, "FastAPI runtime is unavailable on the host")
class HealthRuntimeTests(unittest.TestCase):
    def test_newer_cli_version_completes_check_and_exposes_update_action(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with (
                patch.object(app, "CODEX_HOME", home),
                patch.object(app, "CONFIG_PATH", home / "config.toml"),
                patch.object(app, "AUTH_PATH", home / "auth.json"),
                patch.object(app, "executable_user_cli_path", return_value=Path("/usr/bin/codex")),
                patch.object(app.subprocess, "run", return_value=Mock(stdout="codex-cli 0.160.0\n")),
                patch.object(app, "fetch_latest_cli_version", return_value="0.162.1"),
                patch.object(app, "deployment_key_status", return_value=(True, "已就绪")),
                patch.object(app, "inspect_host_app_server", return_value=("pass", "运行中")),
                patch.object(app, "inspect_session_storage", return_value=("pass", "会话就绪")),
                patch.object(app, "auth_configured", return_value=True),
                patch.object(app, "read_profiles", return_value={}),
                patch.object(app, "active_provider", return_value=""),
                patch.object(app, "audit"),
            ):
                result = app.health_check()
        version = next(item for item in result["checks"] if item["name"] == "Codex CLI 版本")
        self.assertEqual(version["status"], "warning")
        self.assertEqual(version["action"], "update-codex")
        self.assertIn("磁盘空间", [item["name"] for item in result["checks"]])

    def test_failed_live_request_is_a_health_failure(self):
        def diagnose(_profile, on_check):
            on_check({"name": "真实请求", "status": "warning", "detail": "测试「model」失败：HTTP 502"})

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with (
                patch.object(app, "CODEX_HOME", home),
                patch.object(app, "CONFIG_PATH", home / "config.toml"),
                patch.object(app, "AUTH_PATH", home / "auth.json"),
                patch.object(app, "executable_user_cli_path", return_value=None),
                patch.object(app, "deployment_key_status", return_value=(True, "已就绪")),
                patch.object(app, "inspect_host_app_server", return_value=("pass", "运行中")),
                patch.object(app, "inspect_session_storage", return_value=("pass", "会话就绪")),
                patch.object(app, "auth_configured", return_value=True),
                patch.object(app, "read_profiles", return_value={"fhl": {"mode": "pure_api"}}),
                patch.object(app, "active_provider", return_value="fhl"),
                patch.object(app, "normalize_profile", return_value={"mode": "pure_api"}),
                patch.object(app, "diagnose_profile", side_effect=diagnose),
                patch.object(app, "audit"),
            ):
                result = app.health_check()
        request = next(item for item in result["checks"] if item["name"] == "供应商 · 真实请求")
        self.assertEqual(request["status"], "fail")
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
