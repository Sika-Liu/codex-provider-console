import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProviderSchemaTests(unittest.TestCase):
    def test_writes_use_canonical_storage_projection(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        domain = (ROOT / "provider_domain.py").read_text(encoding="utf-8")
        self.assertIn("storage_profile(value)", source)
        self.assertIn('profile["mode"] = mode', domain)
        self.assertIn('profile["protocol"] = protocol', domain)

    def test_status_profiles_redact_credentials(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('result["has_bearer_token"]', source)
        self.assertIn('result.pop("bearer_token", None)', source)
        self.assertIn('result.pop("auth_contents", None)', source)
        self.assertIn("p.mode==='official'", source)
        self.assertIn("p.protocol==='responses'", source)


if __name__ == "__main__":
    unittest.main()
