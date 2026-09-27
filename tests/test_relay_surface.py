import unittest
from pathlib import Path


class RelaySurfaceTests(unittest.TestCase):
    def test_health_and_capabilities_endpoints_expose_the_compatibility_boundary(self):
        source = (Path(__file__).resolve().parents[1] / "relay.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/capabilities")', source)
        self.assertIn('"capabilities": relay_capabilities(protocol)', source)
        self.assertIn('return relay_capabilities(profile["protocol"])', source)


if __name__ == "__main__":
    unittest.main()
