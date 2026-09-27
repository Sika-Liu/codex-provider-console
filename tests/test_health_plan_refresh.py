import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class HealthPlanRefreshTests(unittest.TestCase):
    def test_health_plan_is_reloaded_when_entering_or_running_check(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("loadHealthPlan(true)", source)
        self.assertIn("async function loadHealthPlan(force=false)", source)
        self.assertIn("if(healthPlanLoaded&&!force)return", source)


if __name__ == "__main__":
    unittest.main()
