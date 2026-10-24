import tempfile
import unittest
from pathlib import Path

from application.official_login import DeviceLoginSession


class DeviceLoginSessionTests(unittest.TestCase):
    def test_public_status_hides_device_code_until_login_is_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            session = DeviceLoginSession("official", codex_home=Path(directory))
            session.verification_url = "https://example.test/device"
            session.user_code = "ABCD-EFGH"
            self.assertEqual(session.public_status()["verification_url"], "")
            session.status = "pending"
            status = session.public_status()
            self.assertEqual(status["verification_url"], "https://example.test/device")
            self.assertEqual(status["user_code"], "ABCD-EFGH")


if __name__ == "__main__":
    unittest.main()
