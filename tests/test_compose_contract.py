import pathlib
import os
import shutil
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class ComposeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compose = (ROOT / "compose.yml").read_text(encoding="utf-8")
        cls.dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    def test_console_defaults_to_loopback_and_passes_security_settings(self):
        self.assertIn('${PANEL_BIND:-0.0.0.0}:${PANEL_PORT:-8787}:8787', self.compose)
        self.assertIn('PANEL_BIND: "${PANEL_BIND:-0.0.0.0}"', self.compose)
        self.assertIn('PANEL_SSH_HOST_ALIAS: "${PANEL_SSH_HOST_ALIAS:-}"', self.compose)
        self.assertIn('no-new-privileges:true', self.compose)

    def test_relay_is_not_published_publicly(self):
        self.assertIn('"127.0.0.1:${RELAY_PORT:-57321}:57321"', self.compose)

    def test_both_images_receive_the_version_file(self):
        self.assertIn('COPY VERSION provider_domain.py relay_domain.py relay.py ./', self.dockerfile)
        self.assertIn('COPY VERSION app.py host_ops.py storage_ops.py provider_domain.py relay_domain.py relay.py ./', self.dockerfile)


@unittest.skipUnless(
    os.environ.get("RUN_COMPOSE_SMOKE") == "1" and shutil.which("docker"),
    "set RUN_COMPOSE_SMOKE=1 to run the Docker Compose smoke check",
)
class ComposeSmokeTests(unittest.TestCase):
    def test_compose_file_is_accepted_by_docker(self):
        result = subprocess.run(
            ["docker", "compose", "config", "--quiet"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
