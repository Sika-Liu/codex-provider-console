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


class BackupRetentionPolicyTests(BackupServiceTestCase):
    def test_history_pruning_keeps_latest_complete_and_before_restore(self):
        old_complete = self.snapshot("20260101T000000000000Z", "manual", complete=True)
        new_complete = self.snapshot("20260102T000000000000Z", "manual", complete=True)
        old_before = self.snapshot("20260103T000000000000Z", "before_restore")
        new_before = self.snapshot("20260104T000000000000Z", "before_restore")
        removed = self.service.prune_history()
        self.assertIn(old_complete.name, removed)
        self.assertIn(old_before.name, removed)
        self.assertTrue(new_complete.exists())
        self.assertTrue(new_before.exists())


if __name__ == "__main__":
    unittest.main()
