import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class DockerfileImportsTests(unittest.TestCase):
    def test_console_image_copies_app_packages(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY api security ./", dockerfile)


if __name__ == "__main__":
    unittest.main()
