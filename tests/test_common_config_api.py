import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CommonConfigApiTests(unittest.TestCase):
    def test_common_config_routes_and_persistence_are_present(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('COMMON_CONFIG_PATH = CODEX_HOME / "control-panel-common-config.toml"', source)
        self.assertIn('@app.get("/api/common-config")', source)
        self.assertIn('@app.post("/api/common-config/extract")', source)
        self.assertIn('@app.post("/api/common-config")', source)
        self.assertIn('saved_common = COMMON_CONFIG_PATH.read_text', source)
        self.assertIn('tomllib.loads(contents or "")', source)


if __name__ == "__main__":
    unittest.main()
