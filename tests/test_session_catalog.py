import json
import re
import tempfile
import unittest
from pathlib import Path

from application.session_catalog import SessionCatalog, SessionCatalogPaths


class SessionCatalogTests(unittest.TestCase):
    thread_id = "01234567-89ab-cdef-0123-456789abcdef"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sessions = self.root / "sessions"
        self.archived = self.root / "archived_sessions"
        self.sessions.mkdir()
        self.index = self.root / "session_index.jsonl"
        self.catalog = SessionCatalog(
            SessionCatalogPaths(
                codex_home=self.root,
                sessions_path=self.sessions,
                archived_sessions_path=self.archived,
                session_index_path=self.index,
                thread_id=re.compile(r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
                thread_id_in_text=re.compile(r"(?i)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"),
            )
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_active_rollout_prefers_index_title_and_exposes_metadata(self):
        rollout = self.sessions / f"rollout-{self.thread_id}.jsonl"
        rollout.write_text(json.dumps({"payload": {"title": "rollout title", "cwd": "/project", "model_provider": "custom"}}) + "\n", encoding="utf-8")
        self.index.write_text(json.dumps({"id": self.thread_id, "thread_name": "index title"}) + "\n", encoding="utf-8")

        sessions = self.catalog.list_active()

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["id"], self.thread_id)
        self.assertEqual(sessions[0]["title"], "index title")
        self.assertEqual(sessions[0]["cwd"], "/project")
        self.assertEqual(sessions[0]["provider_id"], "custom")
        self.assertEqual(sessions[0]["kind"], "rollout")

    def test_index_entry_without_rollout_is_reported_as_orphaned(self):
        self.index.write_text(json.dumps({"id": self.thread_id, "thread_name": "saved", "updated_at": "2026-10-24T00:00:00Z"}) + "\n", encoding="utf-8")

        sessions = self.catalog.list_active()

        self.assertEqual(sessions[0]["kind"], "orphaned_index")
        self.assertEqual(sessions[0]["title"], "saved")


if __name__ == "__main__":
    unittest.main()
