import unittest
from pathlib import Path

from provider_domain import (
    remote_session_archive_args,
    remote_session_delete_args,
    remote_session_unarchive_args,
    resolve_host_codex_home,
)


class HostSessionDeletionTests(unittest.TestCase):
    def test_prefers_explicit_host_codex_home(self):
        self.assertEqual(resolve_host_codex_home("/srv/codex-data", "/home/ubuntu"), "/srv/codex-data")

    def test_uses_deployment_user_home_as_default(self):
        self.assertEqual(resolve_host_codex_home("", "/home/ubuntu"), "/home/ubuntu/.codex")

    def test_rejects_a_non_absolute_host_codex_home(self):
        with self.assertRaises(ValueError):
            resolve_host_codex_home("relative/.codex", "/home/ubuntu")

    def test_session_deletion_must_use_managed_app_server(self):
        thread_id = "01a09344-939e-7382-9f00-7347bf6ccf37"
        self.assertEqual(
            remote_session_delete_args(thread_id),
            ["delete", "--remote", "unix://", "--force", thread_id],
        )

    def test_session_archiving_must_use_managed_app_server(self):
        thread_id = "01a09344-939e-7382-9f00-7347bf6ccf37"
        self.assertEqual(remote_session_archive_args(thread_id), ["archive", "--remote", "unix://", thread_id])
        self.assertEqual(remote_session_unarchive_args(thread_id), ["unarchive", "--remote", "unix://", thread_id])

    def test_permanent_delete_does_not_create_a_hidden_backup(self):
        source = Path(__file__).resolve().parents[1].joinpath("app.py").read_text(encoding="utf-8")
        endpoint = source.split('@app.delete("/api/sessions/{thread_id}")', 1)[1].split(
            '@app.post("/api/sessions/{thread_id}/trash")', 1
        )[0]
        self.assertNotIn("backup_state", endpoint)
        self.assertIn('"recoverable": False', endpoint)

    def test_session_page_exposes_both_delete_modes_and_restore(self):
        source = Path(__file__).resolve().parents[1].joinpath("app.py").read_text(encoding="utf-8")
        self.assertIn("移入回收站", source)
        self.assertIn("永久删除不会创建备份", source)
        self.assertIn("session-restore", source)
        self.assertIn('@app.post("/api/session-trash/{trash_id}/restore")', source)
        self.assertIn('@app.delete("/api/session-trash")', source)
        self.assertIn("清空回收站", source)
        self.assertIn("reap_expired_session_trash", source)
        self.assertIn("已归档会话", source)
        self.assertIn('@app.get("/api/archived-sessions")', source)
        self.assertIn('@app.post("/api/sessions/{thread_id}/archive")', source)
        self.assertIn('@app.post("/api/archived-sessions/{thread_id}/unarchive")', source)
