import unittest
from pathlib import Path


class ProviderBackupRetentionTests(unittest.TestCase):
    def test_only_managed_provider_switch_backups_are_pruned_to_five(self):
        source = Path(__file__).resolve().parents[1].joinpath("app.py").read_text(encoding="utf-8")
        self.assertIn("PROVIDER_SWITCH_BACKUP_RETENTION = 5", source)
        self.assertIn('backup_state(include_sessions=True, reason="provider_switch")', source)
        self.assertIn("def prune_provider_switch_backups", source)
        self.assertIn('metadata.get("reason") == "provider_switch"', source)
        self.assertIn("pruned_backup_ids = prune_provider_switch_backups()", source)


if __name__ == "__main__":
    unittest.main()
