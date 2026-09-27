import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SshHostVerificationTests(unittest.TestCase):
    def test_all_host_operations_use_the_shared_strict_ssh_builder(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        host_ops = (ROOT / "host_ops.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("host_ssh_command(gateway),"), 4)
        self.assertIn('"StrictHostKeyChecking=yes"', host_ops)
        self.assertNotIn("StrictHostKeyChecking=accept-new", source + host_ops)

    def test_missing_or_unknown_host_key_fails_closed(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        host_ops = (ROOT / "host_ops.py").read_text(encoding="utf-8")
        self.assertIn("ssh-keygen", host_ops)
        self.assertIn("未找到宿主机 SSH 指纹", host_ops)
        self.assertIn("宿主机 SSH 指纹尚未登记", host_ops)

    def test_host_alias_is_passed_into_the_panel(self):
        compose = (ROOT / "compose.yml").read_text(encoding="utf-8")
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("PANEL_SSH_HOST_ALIAS", compose)
        self.assertIn("PANEL_SSH_HOST_ALIAS=", env_example)


if __name__ == "__main__":
    unittest.main()
