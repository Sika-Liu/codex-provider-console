import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

from relay_domain import responses_to_chat_request


class RelayHttpIntegrationTests(unittest.TestCase):
    def test_chat_conversion_reaches_mock_upstream(self):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                seen["body"] = json.loads(self.rfile.read(length))
                payload = json.dumps({
                    "id": "chat-test",
                    "object": "chat.completion",
                    "model": seen["body"].get("model"),
                    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = {"model": "mock-model", "input": "hi", "max_output_tokens": 1}
            converted = responses_to_chat_request(body)
            request = Request(
                f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                data=json.dumps(converted).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                payload = json.loads(response.read())
            self.assertEqual(payload["choices"][0]["message"]["content"], "ok")
            self.assertEqual(seen["body"]["messages"][0]["content"], "hi")
        finally:
            server.shutdown()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

