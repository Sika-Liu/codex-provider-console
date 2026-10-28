import unittest

from app import redacted_auth_snapshot


class AuthRedactionTests(unittest.TestCase):
    def test_masks_reusable_credentials_and_keeps_account_metadata(self):
        result = redacted_auth_snapshot(
            '{"auth_mode":"chatgpt","account_id":"acct-123",'
            '"tokens":{"access_token":"access-secret","refresh_token":"refresh-secret"}}'
        )
        self.assertIn('"account_id": "acct-123"', result)
        self.assertIn('"tokens": "***"', result)


if __name__ == "__main__":
    unittest.main()
