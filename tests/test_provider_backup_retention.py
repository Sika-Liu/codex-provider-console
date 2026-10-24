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


class ProviderBackupRetentionTests(BackupServiceTestCase):
    def test_only_provider_switch_backups_are_pruned(self):
        unmanaged = self.snapshot("20260101T000000000000Z", "manual")
        managed = [
            self.snapshot(f"2026010{index}T000000000000Z", "provider_switch")
            for index in range(2, 8)
        ]
        removed = self.service.prune_provider_switch_backups(retain=5)
        self.assertEqual(removed, [managed[0].name])
        self.assertTrue(unmanaged.exists())
        self.assertTrue(all(path.exists() for path in managed[2:]))


if __name__ == "__main__":
    unittest.main()
