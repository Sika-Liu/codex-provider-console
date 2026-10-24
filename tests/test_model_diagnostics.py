import unittest
from pathlib import Path


class ModelDiagnosticTests(unittest.TestCase):
    def test_response_shape_validation_is_explicit(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "infrastructure" / "upstream_http.py").read_text(encoding="utf-8")
        app_source = (root / "app.py").read_text(encoding="utf-8")
        self.assertIn("from model_diagnostic_domain import validate_model_diagnostic_response", source)
        self.assertIn('"error_kind": "" if shape_ok else "invalid_response"', source)
        self.assertIn("MODEL_DIAGNOSTIC_MAX_MODELS", app_source)

    def test_rate_limit_handling_is_explicit(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "infrastructure" / "upstream_http.py").read_text(encoding="utf-8")
        app_source = (root / "app.py").read_text(encoding="utf-8")
        self.assertIn('status": exc.code', source)
        self.assertIn('"error_kind": kind', source)
        self.assertIn('result_kind == "rate_limited"', app_source)
        self.assertIn('"retry_after": retry_after', source)

    def test_source_stops_batch_after_rate_limit_and_reports_skipped(self):
        source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
        self.assertIn('"status": "skipped"', source)
        self.assertIn("上游限流，已停止后续诊断", source)
        self.assertIn("usable, usability_detail = is_profile_usable(profile)", source)


if __name__ == "__main__":
    unittest.main()
