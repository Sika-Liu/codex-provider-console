import unittest

from api.compat import AuthService, ProviderStore
from application.provider_store import ProviderStore as ProviderStoreImplementation
from security.auth_service import AuthService as AuthServiceImplementation


class CompatibilityEntrypointTests(unittest.TestCase):
    def test_api_compat_exports_stable_service_types(self):
        self.assertIs(AuthService, AuthServiceImplementation)
        self.assertIs(ProviderStore, ProviderStoreImplementation)


if __name__ == "__main__":
    unittest.main()
