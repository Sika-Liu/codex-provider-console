import unittest

from application.provider_activation import ProviderActivationValidationError, build_activation_plan


class ProviderActivationPlanTests(unittest.TestCase):
    def pure_api(self, **overrides):
        profile = {
            "mode": "pure_api",
            "auth_mode": "apikey",
            "bearer_token": "secret-key",
            "auth_contents": "",
            "model": "gpt-test",
        }
        profile.update(overrides)
        return profile

    def test_pure_api_uses_relay_and_never_writes_upstream_key_to_config(self):
        plan = build_activation_plan(
            self.pure_api(),
            base_root='approval_policy = "never"',
            merged_sections='[features]\ngoals = true',
            session_provider_id="custom",
            host_relay_base_url="http://127.0.0.1:57321/v1",
        )
        self.assertIn('model_provider = "custom"', plan.config_text)
        self.assertIn('base_url = "http://127.0.0.1:57321/v1"', plan.config_text)
        self.assertIn('experimental_bearer_token = "codex-provider-console-relay"', plan.config_text)
        self.assertNotIn("secret-key", plan.config_text)
        self.assertIn('"OPENAI_API_KEY": "secret-key"', plan.auth_text)
        self.assertEqual(plan.selected_model, "gpt-test")

    def test_official_profile_requires_chatgpt_snapshot(self):
        profile = {"mode": "official", "auth_mode": "chatgpt", "auth_contents": '{"auth_mode": "apikey"}'}
        with self.assertRaisesRegex(ProviderActivationValidationError, "ChatGPT login"):
            build_activation_plan(
                profile,
                base_root="",
                merged_sections="",
                session_provider_id="custom",
                host_relay_base_url="http://127.0.0.1:57321/v1",
            )

    def test_pure_api_requires_key_unless_explicitly_disabled(self):
        with self.assertRaisesRegex(ProviderActivationValidationError, "API key"):
            build_activation_plan(
                self.pure_api(bearer_token=None),
                base_root="",
                merged_sections="",
                session_provider_id="custom",
                host_relay_base_url="http://127.0.0.1:57321/v1",
            )

    def test_model_override_wins_over_profile_default(self):
        plan = build_activation_plan(
            self.pure_api(),
            base_root="",
            merged_sections="",
            session_provider_id="custom",
            host_relay_base_url="http://127.0.0.1:57321/v1",
            model_override="gpt-override",
        )
        self.assertEqual(plan.selected_model, "gpt-override")
        self.assertIn('model = "gpt-override"', plan.config_text)


if __name__ == "__main__":
    unittest.main()
