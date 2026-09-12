import unittest

from provider_domain import remote_session_delete_args, resolve_host_codex_home


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
