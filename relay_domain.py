"""Protocol conversion primitives for the remote pure-API relay."""

from __future__ import annotations

from typing import Any


def resolve_active_profile(
    profiles: object,
    config: object,
    settings: object,
) -> dict[str, Any] | None:
    """Find the panel profile behind Codex's stable session identity.

    `config.toml` deliberately uses the stable provider id ``custom`` so that
    conversations created before a supplier switch remain resumable. The actual
    supplier id is stored separately by the control panel. Config lookup remains
    available for installations created before the stable identity change.
    """
    if not isinstance(profiles, dict):
        return None
    selected = settings.get("active_provider_id") if isinstance(settings, dict) else None
    if isinstance(selected, str) and isinstance(profiles.get(selected), dict):
        return profiles[selected]
    legacy_provider = config.get("model_provider") if isinstance(config, dict) else None
    if isinstance(legacy_provider, str) and isinstance(profiles.get(legacy_provider), dict):
        return profiles[legacy_provider]
    return None
import json
from collections.abc import Iterable, Iterator
from typing import Any


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks: list[str] = []
        for item in value:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    chunks.append(item["text"])
                elif isinstance(item.get("content"), str):
                    chunks.append(item["content"])
        return "".join(chunks)
    return ""


def responses_to_chat_request(body: dict[str, Any]) -> dict[str, Any]:
    """Translate the text/tool subset required for the initial relay release."""
    messages: list[dict[str, Any]] = []
    instructions = _text(body.get("instructions"))
    if instructions:
        messages.append({"role": "system", "content": instructions})
    raw_input = body.get("input", "")
    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
    elif isinstance(raw_input, list):
        for item in raw_input:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                if item.get("type") == "function_call_output":
                    messages.append({"role": "tool", "tool_call_id": str(item.get("call_id", "")), "content": _text(item.get("output", ""))})
                    continue
                role = item.get("role") if item.get("role") in {"system", "developer", "user", "assistant"} else "user"
                content = _text(item.get("content", item.get("text", "")))
                if content:
                    messages.append({"role": role, "content": content})
    converted: dict[str, Any] = {"model": body.get("model", ""), "messages": messages}
    if "max_output_tokens" in body:
        converted["max_tokens"] = body["max_output_tokens"]
    for key in ("temperature", "top_p", "stop", "seed", "user"):
        if key in body:
            converted[key] = body[key]
    if isinstance(body.get("tools"), list):
        tools = []
        for tool in body["tools"]:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
                tools.append(tool)
            elif tool.get("type") == "function":
                tools.append({"type": "function", "function": {key: tool[key] for key in ("name", "description", "parameters") if key in tool}})
        if tools:
            converted["tools"] = tools
    if body.get("tool_choice"):
        converted["tool_choice"] = body["tool_choice"]
    return converted


def chat_to_response(body: dict[str, Any]) -> dict[str, Any]:
    """Convert a non-streaming Chat Completions response to a Responses body."""
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("chat response is missing choices[0]")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ValueError("chat response choice is missing message")
    content = _text(message.get("content", ""))
    output: list[dict[str, Any]] = []
    if content:
        output.append({"type": "message", "id": f"msg_{body.get('id', 'relay')}", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": content, "annotations": []}]})
    for call in message.get("tool_calls", []) if isinstance(message.get("tool_calls"), list) else []:
        function = call.get("function") if isinstance(call, dict) and isinstance(call.get("function"), dict) else {}
        call_id = str(call.get("id", "")) if isinstance(call, dict) else ""
        name = str(function.get("name", ""))
        if call_id and name:
            output.append({"type": "function_call", "id": call_id, "call_id": call_id, "name": name, "arguments": str(function.get("arguments", "{}")), "status": "completed"})
    return {"id": f"resp_{body.get('id', 'relay')}", "object": "response", "status": "completed", "model": body.get("model", ""), "output": output, "usage": body.get("usage", {})}


def _sse(event: str, payload: dict[str, Any]) -> bytes:
    """Encode one Responses-style server-sent event."""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")


def chat_sse_to_responses_events(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Translate the common OpenAI-compatible Chat Completions SSE subset.

    The conversion intentionally ignores provider-specific metadata instead of
    leaking it into the Responses wire format. Text and function-call argument
    deltas are retained, which is the minimum needed for agent tool loops.
    """
    response_id = "resp_relay"
    model = ""
    created = False
    output_index = 0
    text_started = False
    text_output_index = -1
    text_parts: list[str] = []
    function_calls: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] = {}

    def ensure_created() -> Iterator[bytes]:
        nonlocal created
        if not created:
            created = True
            response = {"id": response_id, "object": "response", "status": "in_progress", "model": model, "output": []}
            yield _sse("response.created", {"type": "response.created", "response": response})

    buffer = b""
    for chunk in chunks:
        buffer += chunk.replace(b"\r\n", b"\n")
        while b"\n\n" in buffer:
            raw_event, buffer = buffer.split(b"\n\n", 1)
            data_lines = [line[5:].strip() for line in raw_event.splitlines() if line.startswith(b"data:")]
            if not data_lines:
                continue
            data = b"\n".join(data_lines)
            if data == b"[DONE]":
                continue
            try:
                event = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            response_id = f"resp_{event.get('id', 'relay')}"
            model = str(event.get("model") or model)
            if isinstance(event.get("usage"), dict):
                usage = event["usage"]
            choices = event.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                continue
            delta = choices[0].get("delta")
            if not isinstance(delta, dict):
                continue
            for encoded in ensure_created():
                yield encoded
            content = _text(delta.get("content"))
            if content:
                if not text_started:
                    text_started = True
                    text_output_index = output_index
                    output_index += 1
                    message = {"type": "message", "id": f"msg_{response_id}", "status": "in_progress", "role": "assistant", "content": []}
                    yield _sse("response.output_item.added", {"type": "response.output_item.added", "output_index": text_output_index, "item": message})
                    yield _sse("response.content_part.added", {"type": "response.content_part.added", "output_index": text_output_index, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}})
                text_parts.append(content)
                yield _sse("response.output_text.delta", {"type": "response.output_text.delta", "output_index": text_output_index, "content_index": 0, "delta": content})
            calls = delta.get("tool_calls")
            if isinstance(calls, list):
                for raw_call in calls:
                    if not isinstance(raw_call, dict):
                        continue
                    index = int(raw_call.get("index", len(function_calls)))
                    call = function_calls.setdefault(index, {"id": str(raw_call.get("id") or f"call_{index}"), "name": "", "arguments": "", "announced": False})
                    function = raw_call.get("function") if isinstance(raw_call.get("function"), dict) else {}
                    call["name"] = str(function.get("name") or call["name"])
                    arguments = str(function.get("arguments") or "")
                    if not call["announced"] and call["name"]:
                        call["announced"] = True
                        call["output_index"] = output_index
                        output_index += 1
                        item = {"type": "function_call", "id": call["id"], "call_id": call["id"], "name": call["name"], "arguments": "", "status": "in_progress"}
                        yield _sse("response.output_item.added", {"type": "response.output_item.added", "output_index": call["output_index"], "item": item})
                    if arguments and call["announced"]:
                        call["arguments"] += arguments
                        yield _sse("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta", "output_index": call["output_index"], "delta": arguments})
    for encoded in ensure_created():
        yield encoded
    completed_output: list[dict[str, Any]] = []
    if text_started:
        text = "".join(text_parts)
        item = {"type": "message", "id": f"msg_{response_id}", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": text, "annotations": []}]}
        yield _sse("response.content_part.done", {"type": "response.content_part.done", "output_index": text_output_index, "content_index": 0, "part": item["content"][0]})
        yield _sse("response.output_item.done", {"type": "response.output_item.done", "output_index": text_output_index, "item": item})
        completed_output.append(item)
    for call in function_calls.values():
        if call["announced"]:
            item = {"type": "function_call", "id": call["id"], "call_id": call["id"], "name": call["name"], "arguments": call["arguments"], "status": "completed"}
            yield _sse("response.function_call_arguments.done", {"type": "response.function_call_arguments.done", "output_index": call["output_index"], "arguments": call["arguments"]})
            yield _sse("response.output_item.done", {"type": "response.output_item.done", "output_index": call["output_index"], "item": item})
            completed_output.append(item)
    response = {"id": response_id, "object": "response", "status": "completed", "model": model, "output": completed_output, "usage": usage}
    yield _sse("response.completed", {"type": "response.completed", "response": response})
