import unittest
import hashlib

import pyotp

from application.totp_service import TOTPError, TOTPService


class TOTPServiceTests(unittest.TestCase):
    def setUp(self):
        self.settings = {}
        self.service = TOTPService(
            read_settings=lambda: dict(self.settings),
            write_settings=lambda value: self.settings.update(value),
            session_secret=lambda: "session-secret",
            password_matches=lambda value: value == "old-password",
            password_reset=lambda value: setattr(self, "new_password", value),
            audit=lambda *args, **kwargs: None,
        )

    def test_setup_requires_confirmation_before_persisting(self):
        result = self.service.setup("admin")
        self.assertIn("secret", result)
        self.assertIn("otpauth://", result["provisioning_uri"])
        self.assertEqual(result["account_name"], "admin")
        self.assertEqual(result["issuer"], "Codex 控制台")
        self.assertIn("<svg", result["qr_svg"])
        self.assertNotIn("panel_totp_secret", self.settings)
        self.service.confirm(pyotp.TOTP(result["secret"]).now())
        self.assertEqual(self.settings["panel_totp_secret"], result["secret"])

    def test_recovery_issues_one_time_reset_token(self):
        secret = pyotp.random_base32()
        self.settings["panel_totp_secret"] = secret
        result = self.service.recover("admin", pyotp.TOTP(secret).now(), "client")
        self.service.complete_reset(result["reset_token"], "new-password")
        self.assertEqual(self.new_password, "new-password")
        with self.assertRaises(TOTPError):
            self.service.complete_reset(result["reset_token"], "another-password")

    def test_recovery_code_is_generated_hashed_and_one_time(self):
        self.service.write_settings = lambda value: (self.settings.clear(), self.settings.update(value))
        result = self.service.generate_recovery_code()
        self.assertGreaterEqual(len(result["recovery_code"]), 40)
        self.assertEqual(
            self.settings["panel_recovery_code_hash"],
            hashlib.sha256(result["recovery_code"].encode()).hexdigest(),
        )
        reset = self.service.recover("admin", result["recovery_code"], "client")
        self.assertNotIn("panel_recovery_code_hash", self.settings)
        self.service.complete_reset(reset["reset_token"], "new-password")
        with self.assertRaises(TOTPError):
            self.service.recover("admin", result["recovery_code"], "client-2")

    def test_wrong_recovery_code_is_rejected(self):
        secret = pyotp.random_base32()
        self.settings["panel_totp_secret"] = secret
        with self.assertRaises(TOTPError):
            self.service.recover("admin", "000000", "client")


if __name__ == "__main__":
    unittest.main()
