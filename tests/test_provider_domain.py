import unittest

from provider_domain import backfill_profile_model, is_profile_usable, normalize_profile


class ProviderDomainTests(unittest.TestCase):
    def test_legacy_api_profile_migrates_to_pure_api(self):
        profile = normalize_profile(
            {"id": "example", "name": "Example", "auth_mode": "apikey", "wire_api": "chat"}
        )
        self.assertEqual(profile["schema_version"], 2)
        self.assertEqual(profile["mode"], "pure_api")
        self.assertEqual(profile["protocol"], "chat_completions")
        self.assertEqual(profile["wire_api"], "chat")

    def test_official_mode_removes_custom_transport(self):
        profile = normalize_profile(
            {"id": "official", "name": "Official", "mode": "official", "base_url": "https://x", "bearer_token": "secret"}
        )
        self.assertEqual(profile["base_url"], "")
        self.assertIsNone(profile["bearer_token"])
        self.assertEqual(profile["protocol"], "responses")

    def test_null_v2_fields_fall_back_to_legacy_official_mode(self):
        profile = normalize_profile({"id": "official", "name": "Official", "mode": None, "auth_mode": "chatgpt"})
        self.assertEqual(profile["mode"], "official")

    def test_normalization_discards_removed_model_catalog_settings(self):
        profile = normalize_profile(
            {
                "id": "example",
                "name": "Example",
                "model_windows": {"gpt-test": "1M"},
                "model_auto_compact": {"gpt-test": "800K"},
                "model_metadata": {"gpt-test": {"context_window": "1M"}},
            }
        )
        self.assertNotIn("model_windows", profile)
        self.assertNotIn("model_auto_compact", profile)
        self.assertNotIn("model_metadata", profile)

    def test_normalization_discards_legacy_hidden_test_model(self):
        profile = normalize_profile({"id": "example", "name": "Example", "model": "gpt-5.5", "test_model": "codex-auto-review"})
        self.assertEqual(profile["model"], "gpt-5.5")
        self.assertNotIn("test_model", profile)

    def test_pure_api_requires_url_and_key_unless_no_auth(self):
        usable, reason = is_profile_usable({"id": "x", "name": "X", "mode": "pure_api"})
        self.assertFalse(usable)
        self.assertIn("base URL", reason)
        usable, reason = is_profile_usable(
            {"id": "x", "name": "X", "mode": "pure_api", "base_url": "http://127.0.0.1", "no_auth": True}
        )
        self.assertTrue(usable, reason)

    def test_backfill_uses_live_model_without_mutating_original_profile(self):
        original = {"id": "x", "name": "X", "model": "gpt-old", "auth_mode": "apikey"}
        updated, changed = backfill_profile_model(original, " gpt-new ")
        self.assertTrue(changed)
        self.assertEqual(updated["model"], "gpt-new")
        self.assertEqual(original["model"], "gpt-old")

    def test_backfill_ignores_an_empty_or_unchanged_live_model(self):
        profile = {"id": "x", "name": "X", "model": "gpt-current", "auth_mode": "apikey"}
        _, changed = backfill_profile_model(profile, "")
        self.assertFalse(changed)
        _, changed = backfill_profile_model(profile, "gpt-current")
        self.assertFalse(changed)
