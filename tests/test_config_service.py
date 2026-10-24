import unittest

from application.config_service import (
    canonicalize_base_config,
    normalize_features_sections,
    remove_provider_sections,
    set_goals_feature,
    split_toml_document,
)


class ConfigServiceTests(unittest.TestCase):
    def test_removes_generated_provider_sections_and_root_provider_keys(self):
        config = '''model_provider = "custom"
model = "gpt-test"
[model_providers.custom]
name = "custom"
[features]
goals = true
'''
        cleaned = remove_provider_sections(config)
        self.assertNotIn("model_provider", cleaned)
        self.assertNotIn("model_providers.custom", cleaned)
        self.assertIn("[features]", cleaned)

    def test_normalizes_duplicate_features_and_misplaced_root_keys(self):
        config = '''model = "first"
[features]
goals = false
[features]
goals = true
model = "last"
'''
        normalized = canonicalize_base_config(config)
        self.assertEqual(normalized.count("[features]"), 1)
        self.assertNotIn('model = "first"', normalized)
        self.assertNotIn('model = "last"', normalized)
        self.assertIn("goals = true", normalized)

    def test_goals_update_preserves_root_and_sections(self):
        config = 'approval_policy = "never"\n[features]\ngoals = false'
        updated = set_goals_feature(config, True)
        root, sections = split_toml_document(updated)
        self.assertIn('approval_policy = "never"', root)
        self.assertIn("goals = true", sections)


if __name__ == "__main__":
    unittest.main()
