import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BackupRecoveryTests(unittest.TestCase):
    def test_backups_have_metadata_and_unique_microsecond_ids(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('"include_sessions": include_sessions', source)
        self.assertIn("%Y%m%dT%H%M%S%fZ", source)
        self.assertIn('reason or "manual"', source)

    def test_backup_listing_exposes_reason_contents_and_session_scope(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/api/backups")', source)
        self.assertIn('"created_at": metadata.get', source)
        self.assertIn('"include_sessions": bool', source)
        self.assertIn('"files": sorted(files)', source)

    def test_restore_creates_a_before_restore_snapshot_and_validates_ids(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        storage = (ROOT / "storage_ops.py").read_text(encoding="utf-8")
        self.assertIn("def backup_directory(backup_id: str)", source)
        self.assertIn("identifier.fullmatch(backup_id)", storage)
        self.assertIn('@app.post("/api/backups/{backup_id}/restore")', source)
        self.assertIn('reason="before_restore"', source)

    def test_ui_requires_confirmation_before_restoring(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("备份恢复", source)
        self.assertIn("恢复前会自动备份当前状态", source)
        self.assertIn("restoreBackup", source)


if __name__ == "__main__":
    unittest.main()
