"""Pure response-shape checks for real model diagnostics."""

import json


def validate_model_diagnostic_response(body_text: str, wire_api: str) -> tuple[bool, str]:
    """Require an actual assistant output, not merely HTTP 200 or valid JSON."""
    if not body_text.strip():
        return False, "响应内容为空"
    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError:
        return False, "上游返回的不是有效 JSON"
    if not isinstance(payload, dict):
        return False, "上游响应不是 JSON 对象"
    if "error" in payload and payload["error"] is not None:
        return False, "上游返回错误对象"
    if wire_api == "chat":
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return False, "Chat Completions 响应缺少 choices"
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            return False, "Chat Completions 响应缺少 message"
        content = message.get("content")
        text = content if isinstance(content, str) else "".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ) if isinstance(content, list) else ""
        calls = message.get("tool_calls")
        has_call = isinstance(calls, list) and any(
            isinstance(call, dict) and isinstance(call.get("function"), dict)
            and call["function"].get("name") for call in calls
        )
        if text.strip():
            return True, "已生成文本"
        if has_call:
            return True, "已生成工具调用"
        return False, "请求成功，但模型未产生可验证输出"
    if payload.get("object") != "response":
        return False, "Responses 响应 object 字段不正确"
    if payload.get("status") not in {None, "completed"}:
        return False, f'模型响应未完成：{payload["status"]}'
    output = payload.get("output")
    if not isinstance(output, list):
        return False, "Responses 响应缺少 output"
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call" and item.get("name"):
            return True, "已生成工具调用"
        if item.get("type") == "message":
            for part in item.get("content", []):
                if isinstance(part, dict) and part.get("type") in {"output_text", "text"} and str(part.get("text") or "").strip():
                    return True, "已生成文本"
    return False, "请求成功，但模型未产生可验证输出"

