import unittest

from provider_domain import resolve_host_codex_home


class HostSessionDeletionTests(unittest.TestCase):
    def test_prefers_explicit_host_codex_home(self):
        self.assertEqual(resolve_host_codex_home("/srv/codex-data", "/home/ubuntu"), "/srv/codex-data")

    def test_uses_deployment_user_home_as_default(self):
        self.assertEqual(resolve_host_codex_home("", "/home/ubuntu"), "/home/ubuntu/.codex")

    def test_rejects_a_non_absolute_host_codex_home(self):
        with self.assertRaises(ValueError):
            resolve_host_codex_home("relative/.codex", "/home/ubuntu")
