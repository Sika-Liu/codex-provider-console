import ast
from pathlib import Path
import unittest


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def assigned_string(name: str) -> str:
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"Missing assignment for {name}")


class AppServerLifecycleTests(unittest.TestCase):
    def test_stop_waits_even_when_managed_stop_succeeds(self):
        script = assigned_string("HOST_APP_SERVER_STOP_SCRIPT")
        compile(script, "HOST_APP_SERVER_STOP_SCRIPT", "exec")

        self.assertNotIn("if managed.returncode == 0:\n    print", script)
        self.assertIn("while time.monotonic() < deadline", script)
        self.assertIn("Codex App Server stopped and verified", script)

    def test_start_waits_for_running_daemon_and_socket(self):
        script = assigned_string("HOST_APP_SERVER_START_SCRIPT")
        compile(script, "HOST_APP_SERVER_START_SCRIPT", "exec")

        self.assertIn('["codex", "app-server", "daemon", "version"]', script)
        self.assertIn('payload.get("status") == "running"', script)
        self.assertIn("stat.S_ISSOCK", script)
        self.assertIn("Timed out waiting for Codex App Server readiness", script)


if __name__ == "__main__":
    unittest.main()
