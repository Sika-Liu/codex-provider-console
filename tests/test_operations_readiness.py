import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class OperationsReadinessTests(unittest.TestCase):
    def test_diagnostics_are_read_only_and_docs_cover_recovery(self):
        script = (ROOT / "scripts" / "diagnose.py").read_text(encoding="utf-8")
        docs = (ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
        self.assertIn("docker", script)
        self.assertNotIn('"up"', script)
        self.assertNotIn('"down"', script)
        self.assertNotIn('"rm"', script)
        self.assertIn("before_restore", docs)
        self.assertIn("codex-panel update", docs)
        self.assertIn("只读", docs)

    def test_no_tracked_source_contains_common_secret_markers(self):
        for path in (ROOT / "app.py", ROOT / "relay.py", ROOT / "host_ops.py", ROOT / "storage_ops.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("sk-", source)


if __name__ == "__main__":
    unittest.main()
