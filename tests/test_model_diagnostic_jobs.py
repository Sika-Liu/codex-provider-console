import threading
import time
import unittest
from unittest.mock import patch

try:
    import app
except ModuleNotFoundError:
    app = None


@unittest.skipIf(app is None, "FastAPI runtime is unavailable on the host")
class ModelDiagnosticJobTests(unittest.TestCase):
    def setUp(self):
        app.MODEL_DIAGNOSTIC_JOBS.clear()
        self.profile = {
            "id": "demo", "name": "Demo", "base_url": "https://example.test/v1",
            "bearer_token": "secret", "auth_mode": "apikey", "mode": "pure_api",
            "protocol": "chat_completions", "wire_api": "chat", "model": "model-a",
        }
        self.request = app.ModelDiagnosticRequest(
            **self.profile, models=[{"name": "model-a"}, {"name": "model-b"}]
        )

    def wait_finished(self, job_id):
        for _ in range(100):
            result = app.model_diagnostic_snapshot(job_id)
            if result["status"] != "running":
                return result
            time.sleep(0.01)
        self.fail("diagnostic job did not finish")

    def test_direct_and_relay_results_are_separate(self):
        direct = {"ok": True, "status": 200, "detail": "已生成文本"}
        relay = {"ok": False, "status": 502, "detail": "Relay 返回 HTTP 502"}
        with patch.object(app, "read_profiles", return_value={"demo": dict(self.profile)}), \
             patch.object(app, "active_provider", return_value="demo"), \
             patch.object(app, "test_model_request", return_value=direct) as direct_call, \
             patch.object(app, "test_model_request_via_relay", return_value=relay) as relay_call, \
             patch.object(app, "audit"):
            started = app.start_model_diagnostics(self.request)
            result = self.wait_finished(started["id"])
        self.assertEqual(result["passed"], 2)
        self.assertEqual(result["relay_result"]["status"], "fail")
        self.assertEqual(direct_call.call_count, 2)
        relay_call.assert_called_once()

    def test_cancel_skips_remaining_models(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_request(*args, **kwargs):
            entered.set()
            release.wait(2)
            return {"ok": True, "status": 200, "detail": "已生成文本"}

        with patch.object(app, "read_profiles", return_value={}), \
             patch.object(app, "test_model_request", side_effect=slow_request) as direct_call, \
             patch.object(app, "audit"):
            started = app.start_model_diagnostics(self.request)
            self.assertTrue(entered.wait(1))
            app.cancel_model_diagnostics(started["id"])
            release.set()
            result = self.wait_finished(started["id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(direct_call.call_count, 1)

    def test_time_budget_skips_unstarted_requests(self):
        with patch.object(app, "read_profiles", return_value={}), \
             patch.object(app, "MODEL_DIAGNOSTIC_MAX_SECONDS", 0), \
             patch.object(app, "test_model_request") as direct_call, \
             patch.object(app, "audit"):
            started = app.start_model_diagnostics(self.request)
            result = self.wait_finished(started["id"])
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(result["skipped"], 2)
        direct_call.assert_not_called()


@unittest.skipIf(app is None, "FastAPI runtime is unavailable on the host")
class ModelDiagnosticRequestTests(unittest.TestCase):
    def setUp(self):
        self.profile = {"base_url": "https://example.test/v1", "wire_api": "responses", "bearer_token": "secret"}

    def test_http_200_without_output_is_not_a_pass(self):
        class Response:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                return b'{"object":"response","output":[]}'
        with patch.object(app.urllib.request, "urlopen", return_value=Response()):
            result = app.test_model_request(self.profile, "model-a", max_attempts=1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_kind"], "invalid_response")
        self.assertNotIn("body", result)

    def test_http_200_with_text_is_a_pass(self):
        class Response:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                return b'{"object":"response","status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"ok"}]}]}'
        with patch.object(app.urllib.request, "urlopen", return_value=Response()):
            result = app.test_model_request(self.profile, "model-a", max_attempts=1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["detail"], "已生成文本")
        self.assertNotIn("body", result)

    def test_expired_finished_job_is_removed(self):
        app.MODEL_DIAGNOSTIC_JOBS.clear()
        app.MODEL_DIAGNOSTIC_JOBS["old"] = {
            "status": "completed", "updated_at": 1,
        }
        app.prune_model_diagnostic_jobs(now=app.MODEL_DIAGNOSTIC_JOB_TTL_SECONDS + 2)
        self.assertNotIn("old", app.MODEL_DIAGNOSTIC_JOBS)


if __name__ == "__main__":
    unittest.main()
