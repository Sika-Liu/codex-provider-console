import json
import unittest

from model_diagnostic_domain import validate_model_diagnostic_response


class ModelDiagnosticRuntimeTests(unittest.TestCase):
    def check(self, payload, wire_api="responses"):
        return validate_model_diagnostic_response(json.dumps(payload), wire_api)

    def test_responses_requires_actual_output(self):
        self.assertFalse(self.check({"object": "response", "output": []})[0])
        self.assertFalse(self.check({"object": "response", "status": "incomplete", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "partial"}]}
        ]})[0])
        self.assertFalse(self.check({"object": "response", "output": [], "error": {"message": "failed"}})[0])
        self.assertFalse(self.check({"object": "response", "output": [], "error": {}})[0])
        self.assertTrue(self.check({"object": "response", "status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "ok"}]}
        ]})[0])
        self.assertTrue(self.check({"object": "response", "output": [
            {"type": "function_call", "name": "tool"}
        ]})[0])

    def test_chat_requires_message_with_text_or_tool_call(self):
        self.assertFalse(self.check({"choices": [{"message": {"content": ""}}]}, "chat")[0])
        self.assertFalse(self.check({"choices": [{"delta": {"content": "partial"}}]}, "chat")[0])
        self.assertTrue(self.check({"choices": [{"message": {"content": "ok"}}]}, "chat")[0])
        self.assertTrue(self.check({"choices": [{"message": {"tool_calls": [
            {"function": {"name": "tool", "arguments": "{}"}}
        ]}}]}, "chat")[0])

    def test_malformed_payloads_fail(self):
        self.assertFalse(validate_model_diagnostic_response("", "responses")[0])
        self.assertFalse(validate_model_diagnostic_response("not-json", "responses")[0])
        self.assertFalse(self.check({"object": "wrong", "output": []})[0])


if __name__ == "__main__":
    unittest.main()
