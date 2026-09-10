"""Minimal OpenAI-compatible upstream used for container relay integration."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/v1/responses":
            body = {"id": "resp_native", "object": "response", "status": "completed", "model": payload.get("model"), "output": [{"type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "native-ok", "annotations": []}]}]}
        elif self.path == "/v1/chat/completions":
            body = {"id": "chat_mock", "object": "chat.completion", "model": payload.get("model"), "choices": [{"message": {"role": "assistant", "content": "chat-ok"}}]}
        else:
            self.send_error(404)
            return
        encoded = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_):
        pass


HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
