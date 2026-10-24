import json
import re
import tempfile
import unittest
from pathlib import Path

from application.backup_service import BackupService
from storage_ops import write_private


class BackupServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.codex_home = self.root / "codex"
        self.codex_home.mkdir()
        self.config = self.codex_home / "config.toml"
        self.profiles = self.codex_home / "control-panel-profiles.json"
        self.settings = self.codex_home / "control-panel-settings.json"
        self.auth = self.codex_home / "auth.json"
        self.common = self.codex_home / "control-panel-common-config.toml"
        self.session_index = self.codex_home / "session_index.jsonl"
        self.backups = self.codex_home / "backups"
        self.service = BackupService(
            codex_home=self.codex_home,
            backup_root=self.backups,
            metadata_name="metadata.json",
            backup_id_pattern=re.compile(r"^[0-9]{8}T[0-9]{6,12}Z$"),
            config_path=self.config,
            profile_path=self.profiles,
            settings_path=self.settings,
            auth_path=self.auth,
            common_config_path=self.common,
            session_index_path=self.session_index,
            write_private=write_private,
        )

    def tearDown(self):
        self.temp.cleanup()

    def write_panel_state(self):
        for path, value in (
            (self.config, "model = 'first'\n"),
            (self.profiles, '{"p": {}}\n'),
            (self.settings, '{"active_provider_id": "p"}\n'),
            (self.auth, '{"token": "hidden"}\n'),
            (self.common, "project_doc_max_bytes = 1\n"),
        ):
            write_private(path, value)

    def snapshot(self, identifier: str, reason: str, complete: bool = False):
        directory = self.backups / identifier
        directory.mkdir(parents=True)
        write_private(directory / "metadata.json", json.dumps({"reason": reason}) + "\n")
        if complete:
            for source in (self.config, self.profiles, self.settings, self.auth):
                write_private(directory / source.name, "saved\n")
        return directory


class BackupRecoveryTests(BackupServiceTestCase):
    def test_backup_listing_and_restore_preserve_panel_state(self):
        self.write_panel_state()
        backup_id, backup_dir = self.service.backup_state(reason="manual")
        metadata = json.loads((backup_dir / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["reason"], "manual")
        self.assertFalse(metadata["include_sessions"])
        self.assertEqual(self.service.list_backups()[0]["id"], backup_id)
        self.assertIn("config.toml", self.service.list_backups()[0]["files"])
        write_private(self.config, "model = 'changed'\n")
        restored = self.service.restore_state(backup_dir)
        self.assertIn("config.toml", restored)
        self.assertEqual(self.config.read_text(encoding="utf-8"), "model = 'first'\n")

    def test_invalid_or_missing_backup_id_is_rejected(self):
        with self.assertRaises(ValueError):
            self.service.backup_directory("../escape")
        with self.assertRaises(FileNotFoundError):
            self.service.backup_directory("20260101T000000000000Z")


if __name__ == "__main__":
    unittest.main()
