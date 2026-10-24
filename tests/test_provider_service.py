import json
import tempfile
import unittest
from pathlib import Path

from application.provider_service import ProviderService
from application.provider_store import ProviderStore
from provider_domain import normalize_profile
from storage_ops import write_private


class ProviderServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.profiles_path = root / "profiles.json"
        self.settings_path = root / "settings.json"
        self.backup_reasons: list[str] = []
        store = ProviderStore(self.profiles_path, self.settings_path, normalize_profile)
        self.service = ProviderService(
            store=store,
            profile_path=self.profiles_path,
            write_private=write_private,
            create_backup=lambda *, reason: self.backup_reasons.append(reason),
        )

    def tearDown(self):
        self.temp.cleanup()

    def seed(self, profiles: dict):
        write_private(self.profiles_path, json.dumps(profiles) + "\n")

    def pure_api(self, **overrides):
        profile = {
            "id": "example",
            "name": "Example",
            "mode": "pure_api",
            "protocol": "responses",
            "base_url": "https://provider.example",
            "bearer_token": "secret-old",
            "models": [{"name": "gpt-test"}],
            "model": "gpt-test",
        }
        profile.update(overrides)
        return profile

    def test_empty_credential_preserves_existing_secret_and_redacts_result(self):
        self.seed({"example": self.pure_api(auth_contents='{"OPENAI_API_KEY": "secret-old"}')})
        result = self.service.save(self.pure_api(bearer_token=None, auth_contents=""))
        stored = json.loads(self.profiles_path.read_text(encoding="utf-8"))["example"]
        self.assertEqual(stored["bearer_token"], "secret-old")
        self.assertNotIn("bearer_token", result)
        self.assertTrue(result["has_bearer_token"])
        self.assertEqual(self.backup_reasons, ["provider_save"])

    def test_auth_snapshot_key_overrides_form_key(self):
        self.service.save(self.pure_api(bearer_token="entered", auth_contents='{"OPENAI_API_KEY": "from-auth"}'))
        stored = json.loads(self.profiles_path.read_text(encoding="utf-8"))["example"]
        self.assertEqual(stored["bearer_token"], "from-auth")

    def test_model_must_be_in_available_catalog(self):
        with self.assertRaisesRegex(ValueError, "模型列表"):
            self.service.save(self.pure_api(model="missing"))

    def test_delete_rejects_active_or_missing_profile(self):
        self.seed({"example": self.pure_api()})
        with self.assertRaises(RuntimeError):
            self.service.delete("example", active_provider_id="example")
        with self.assertRaises(LookupError):
            self.service.delete("missing", active_provider_id=None)
        self.service.delete("example", active_provider_id=None)
        self.assertEqual(json.loads(self.profiles_path.read_text(encoding="utf-8")), {})
        self.assertEqual(self.backup_reasons, ["provider_delete"])


if __name__ == "__main__":
    unittest.main()
