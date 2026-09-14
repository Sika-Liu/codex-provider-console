import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from session_trash import create_entry, list_entries, remove_entry, restore_entry


THREAD_ID = "01a09344-939e-7382-9f00-7347bf6ccf37"


class SessionTrashTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.codex_home = self.root / "codex"
        self.trash_root = self.codex_home / "trash" / "control-panel-sessions"
        self.index_path = self.codex_home / "session_index.jsonl"
        self.session_path = self.codex_home / "sessions" / "2026" / f"rollout-{THREAD_ID}.jsonl"

    def tearDown(self):
        self.temporary.cleanup()

    def seed_session(self):
        self.session_path.parent.mkdir(parents=True)
        self.session_path.write_text('{"type":"session_meta"}\n', encoding="utf-8")
        record = {"id": THREAD_ID, "thread_name": "可恢复会话"}
        self.index_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        return record

    def test_create_and_restore_round_trip(self):
        record = self.seed_session()
        created = datetime(2026, 9, 14, tzinfo=timezone.utc)
        manifest = create_entry(
            self.trash_root,
            self.codex_home,
            THREAD_ID,
            [self.session_path],
            record,
            retention_days=7,
            now=created,
        )
        self.session_path.unlink()
        self.index_path.unlink()

        restored = restore_entry(self.trash_root, self.codex_home, self.index_path, manifest["id"])

        self.assertEqual(restored["thread_id"], THREAD_ID)
        self.assertTrue(self.session_path.is_file())
        self.assertEqual(json.loads(self.index_path.read_text(encoding="utf-8").strip()), record)
        self.assertFalse((self.trash_root / manifest["id"]).exists())

    def test_restore_never_overwrites_an_existing_session(self):
        record = self.seed_session()
        manifest = create_entry(self.trash_root, self.codex_home, THREAD_ID, [self.session_path], record)

        with self.assertRaises(FileExistsError):
            restore_entry(self.trash_root, self.codex_home, self.index_path, manifest["id"])

        self.assertTrue((self.trash_root / manifest["id"]).is_dir())

    def test_expired_entries_are_purged(self):
        record = self.seed_session()
        created = datetime(2026, 9, 1, tzinfo=timezone.utc)
        manifest = create_entry(
            self.trash_root,
            self.codex_home,
            THREAD_ID,
            [self.session_path],
            record,
            retention_days=7,
            now=created,
        )

        entries = list_entries(self.trash_root, now=created + timedelta(days=8))

        self.assertEqual(entries, [])
        self.assertFalse((self.trash_root / manifest["id"]).exists())

    def test_permanent_purge_removes_only_the_selected_entry(self):
        record = self.seed_session()
        first = create_entry(self.trash_root, self.codex_home, THREAD_ID, [self.session_path], record)
        second = create_entry(self.trash_root, self.codex_home, THREAD_ID, [self.session_path], record)

        removed = remove_entry(self.trash_root, first["id"])

        self.assertEqual(removed["thread_id"], THREAD_ID)
        self.assertFalse((self.trash_root / first["id"]).exists())
        self.assertTrue((self.trash_root / second["id"]).exists())

    def test_rejects_paths_outside_codex_home(self):
        outside = self.root / "outside.jsonl"
        outside.write_text("secret", encoding="utf-8")
        with self.assertRaises(ValueError):
            create_entry(self.trash_root, self.codex_home, THREAD_ID, [outside], None)


if __name__ == "__main__":
    unittest.main()
