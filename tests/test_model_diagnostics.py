import unittest
from pathlib import Path


class ModelDiagnosticTests(unittest.TestCase):
    def test_rate_limit_handling_is_explicit(self):
        source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
        self.assertIn('status": exc.code', source)
        self.assertIn('"error_kind": kind', source)
        self.assertIn('result_kind == "rate_limited"', source)
        self.assertIn('"retry_after": retry_after', source)

    def test_source_stops_batch_after_rate_limit_and_reports_skipped(self):
        source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
        self.assertIn('"status": "skipped"', source)
        self.assertIn("上游限流，已停止后续诊断", source)
        self.assertIn("usable, usability_detail = is_profile_usable(profile)", source)


if __name__ == "__main__":
    unittest.main()
