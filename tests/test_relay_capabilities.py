import unittest

from relay_domain import chat_sse_to_responses_events, relay_capabilities


class RelayCapabilityTests(unittest.TestCase):
    def test_capabilities_make_unsupported_features_explicit(self):
        result = relay_capabilities("chat")
        self.assertTrue(result["conversion"])
        self.assertTrue(result["supported"]["function_calling"])
        self.assertFalse(result["supported"]["images"])
        self.assertFalse(result["supported"]["reasoning_metadata"])

    def test_stream_flushes_an_event_without_a_final_blank_line(self):
        stream = [b'data: {"id":"chat_tail","choices":[{"delta":{"content":"tail"}}]}\n']
        rendered = b"".join(chat_sse_to_responses_events(stream)).decode("utf-8")
        self.assertIn('"delta":"tail"', rendered)
        self.assertIn("event: response.completed", rendered)


if __name__ == "__main__":
    unittest.main()
