import tempfile
import unittest
from pathlib import Path

from application.health_service import HealthDependencies, HealthService


class HealthServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.audits: list[tuple[str, dict]] = []

    def tearDown(self):
        self.temp.cleanup()

    def service(self, *, profiles=None, active_provider=None):
        dependencies = HealthDependencies(
            codex_home=self.root,
            config_path=self.root / "config.toml",
            auth_path=self.root / "auth.json",
            deploy_user="ubuntu",
            auth_enabled=True,
            executable_user_cli_path=lambda: None,
            fetch_latest_cli_version=lambda: "0.0.0",
            cli_version_number=lambda value: value,
            deployment_key_status=lambda: (True, "已就绪"),
            inspect_host_app_server=lambda: ("pass", "运行中"),
            inspect_session_storage=lambda: ("pass", "会话就绪"),
            config_text=lambda: "",
            auth_configured=lambda: True,
            read_profiles=lambda: profiles or {},
            active_provider=lambda: active_provider,
            normalize_profile=lambda profile: dict(profile),
            diagnose_profile=lambda profile, on_check: {"ok": True},
            audit=lambda action, **details: self.audits.append((action, details)),
        )
        return HealthService(dependencies)

    def test_plan_describes_active_pure_api_diagnostics(self):
        plan = self.service(
            profiles={"example": {"mode": "pure_api", "model": "gpt-test"}},
            active_provider="example",
        ).plan()
        names = [item["name"] for item in plan]
        self.assertIn("供应商 · 模型目录", names)
        self.assertIn("供应商 · 配置模型可见性", names)
        self.assertIn("供应商 · 真实请求", names)

    def test_run_records_summary_and_audit_without_an_active_provider(self):
        result = self.service().run()
        names = [item["name"] for item in result["checks"]]
        self.assertIn("Codex 数据目录", names)
        self.assertIn("当前供应商", names)
        self.assertEqual(self.audits[0][0], "health_checked")


if __name__ == "__main__":
    unittest.main()
