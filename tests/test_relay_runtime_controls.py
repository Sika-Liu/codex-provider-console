import threading
import unittest
from unittest.mock import patch

try:
    import relay
    from fastapi.testclient import TestClient
except ImportError:
    relay = None
    TestClient = None


@unittest.skipIf(relay is None, "FastAPI runtime is unavailable on the host")
class RelayRuntimeControlsTests(unittest.TestCase):
    def setUp(self):
        self.profile = {
            "id": "test", "mode": "pure_api", "protocol": "responses",
            "base_url": "https://example.test", "bearer_token": "",
        }

    def test_forwards_a_valid_request_id_and_returns_it(self):
        with (
            patch.object(relay, "active_profile", return_value=self.profile),
            patch.object(relay, "upstream_request", return_value=(200, {"id": "resp"})) as upstream,
            TestClient(relay.app) as client,
        ):
            response = client.post(
                "/v1/responses",
                json={"model": "test", "input": "hello"},
                headers={"X-Request-Id": "trace_123"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-request-id"], "trace_123")
        self.assertEqual(upstream.call_args.args[-1], "trace_123")

    def test_rejects_oversized_request_before_upstream(self):
        with patch.object(relay, "RELAY_MAX_BODY_BYTES", 4), TestClient(relay.app) as client:
            response = client.post("/v1/responses", content=b'{"a":1}', headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)

    def test_rejects_when_concurrency_limit_is_reached(self):
        semaphore = threading.BoundedSemaphore(1)
        semaphore.acquire()
        with (
            patch.object(relay, "RELAY_SEMAPHORE", semaphore),
            patch.object(relay, "active_profile", return_value=self.profile),
            TestClient(relay.app) as client,
        ):
            response = client.post("/v1/responses", json={"model": "test", "input": "hello"})
        self.assertEqual(response.status_code, 429)


if __name__ == "__main__":
    unittest.main()
