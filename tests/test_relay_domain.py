import unittest

from relay_domain import chat_sse_to_responses_events, chat_to_response, resolve_active_profile, responses_to_chat_request


class RelayDomainTests(unittest.TestCase):
    def test_relay_uses_panel_selection_when_sessions_use_stable_custom_provider(self):
        profile = {"id": "fhl", "mode": "pure_api"}
        result = resolve_active_profile(
            {"fhl": profile},
            {"model_provider": "custom"},
            {"active_provider_id": "fhl"},
        )
        self.assertIs(result, profile)

    def test_relay_keeps_legacy_config_lookup_when_panel_setting_is_missing(self):
        profile = {"id": "legacy", "mode": "pure_api"}
        result = resolve_active_profile(
            {"legacy": profile},
            {"model_provider": "legacy"},
            {},
        )
        self.assertIs(result, profile)

    def test_responses_request_becomes_chat_request(self):
        result = responses_to_chat_request({"model": "example", "instructions": "Be brief", "input": "Hello", "max_output_tokens": 20})
        self.assertEqual(result["model"], "example")
        self.assertEqual(result["messages"], [{"role": "system", "content": "Be brief"}, {"role": "user", "content": "Hello"}])
        self.assertEqual(result["max_tokens"], 20)

    def test_chat_response_becomes_responses_body(self):
        result = chat_to_response({"id": "chatcmpl_1", "model": "example", "choices": [{"message": {"content": "Hi"}}]})
        self.assertEqual(result["object"], "response")
        self.assertEqual(result["output"][0]["content"][0]["text"], "Hi")

    def test_function_calls_round_trip_in_the_supported_subset(self):
        request = responses_to_chat_request({"model": "example", "tools": [{"type": "function", "name": "weather", "parameters": {}}], "input": [{"type": "function_call_output", "call_id": "call_1", "output": "sunny"}]})
        self.assertEqual(request["tools"][0]["function"]["name"], "weather")
        self.assertEqual(request["messages"][0]["role"], "tool")
        response = chat_to_response({"choices": [{"message": {"tool_calls": [{"id": "call_1", "function": {"name": "weather", "arguments": "{}"}}]}}]})
        self.assertEqual(response["output"][0]["type"], "function_call")

    def test_chat_stream_becomes_responses_events(self):
        stream = [
            b'data: {"id":"chat_1","model":"example","choices":[{"delta":{"content":"Hel"}}]}\n\n',
            b'data: {"id":"chat_1","model":"example","choices":[{"delta":{"content":"lo"}}]}\n\n',
            b'data: {"id":"chat_1","model":"example","choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"total_tokens":4}}\n\n',
            b'data: [DONE]\n\n',
        ]
        rendered = b"".join(chat_sse_to_responses_events(stream)).decode("utf-8")
        self.assertIn("event: response.created", rendered)
        self.assertEqual(rendered.count("event: response.output_text.delta"), 2)
        self.assertIn('"text":"Hello"', rendered)
        self.assertIn("event: response.completed", rendered)

    def test_chat_tool_stream_preserves_argument_deltas(self):
        stream = [
            b'data: {"id":"chat_2","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"weather","arguments":"{\\\"city\\\":\\\""}}]}}]}\n\n',
            b'data: {"id":"chat_2","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"Paris\\\"}"}}]}}]}\n\n',
        ]
        rendered = b"".join(chat_sse_to_responses_events(stream)).decode("utf-8")
        self.assertIn("response.function_call_arguments.delta", rendered)
        self.assertIn('"arguments":"{\\\"city\\\":\\\"Paris\\\"}"', rendered)
