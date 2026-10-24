import unittest
from application.recovery_service import RecoveryError, RecoveryService


class RecoveryServiceTests(unittest.TestCase):
    def setUp(self):
        self.settings = {}
        self.secret = "session-secret"
        self.sent = []
        self.service = RecoveryService(
            read_settings=lambda: dict(self.settings),
            write_settings=lambda settings: self.settings.update(settings),
            session_secret=lambda: self.secret,
            password_matches=lambda value: value == "old-password",
            password_reset=lambda value: setattr(self, "new_password", value),
            send_email=lambda *args: self.sent.append(args),
            email_ready=lambda: True,
            audit=lambda *args, **kwargs: None,
        )

    def test_binding_keeps_code_out_of_result(self):
        result = self.service.request_binding("user@example.com", "client")
        self.assertTrue(result["sent"])
        self.assertNotIn("code", result)
        self.assertEqual(len(self.sent), 1)

    def test_reset_rejects_unbound_email(self):
        with self.assertRaises(RecoveryError) as caught:
            self.service.request_reset("user@example.com", "client")
        self.assertEqual(caught.exception.status, 400)


if __name__ == "__main__":
    unittest.main()
