import unittest

from model_catalog import build_model_catalog


class ModelCatalogTests(unittest.TestCase):
    def test_deduplicates_models_and_preserves_policy(self):
        result = build_model_catalog({"model": "alpha", "models": [{"name": "alpha", "context_window": "1M"}, {"name": "beta", "context_window": "128K", "auto_compact_limit": "96K"}]})
        self.assertEqual([item["slug"] for item in result["models"]], ["alpha", "beta"])
        self.assertEqual(result["models"][0]["context_window"], 1_000_000)
        self.assertEqual(result["models"][1]["auto_compact_token_limit"], 96_000)

    def test_catalog_requires_an_explicit_valid_context_limit(self):
        result = build_model_catalog({"model": "example", "models": [{"name": "invalid", "context_window": "many"}]})
        self.assertEqual(result, {"models": []})
