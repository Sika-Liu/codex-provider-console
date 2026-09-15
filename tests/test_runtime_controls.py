import unittest
from pathlib import Path


class RuntimeControlsTests(unittest.TestCase):
    def test_manual_codex_restart_is_not_exposed_by_the_panel(self):
        source = Path(__file__).resolve().parents[1].joinpath("app.py").read_text(encoding="utf-8")
        self.assertIn(".replace('<button class=\"btn\" onclick=\"restartHint()\">重启 Codex</button>', '')", source)
        self.assertNotIn('@app.post("/api/runtime/restart")', source)
        self.assertNotIn("function restartManagedCodex()", source)

