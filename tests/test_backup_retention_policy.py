import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class BackupRetentionPolicyTests(unittest.TestCase):
    def test_history_pruning_keeps_complete_and_before_restore_points(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("def prune_backup_history", source)
        self.assertIn("retain_complete: int = 1", source)
        self.assertIn("retain_before_restore: int = 1", source)
        self.assertIn('item[1].get("reason") == "before_restore"', source)
        self.assertIn("removed = prune_backup_history()", source)


if __name__ == "__main__":
    unittest.main()
