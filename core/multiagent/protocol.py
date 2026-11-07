"""Model-message validation shared by orchestration and transport adapters."""


REASONING_FIELDS = ("reasoning_content", "reasoning", "thinking")


def readable_reasoning(message):
    """Project explicitly returned text; opaque signatures are never display text."""
    for key in REASONING_FIELDS:
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def validate_message(message):
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ValueError("expected assistant message")
    if message.get("content") is not None and not isinstance(message["content"], str):
        raise ValueError("expected string content")
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list) or len(calls) > 16:
        raise ValueError("expected at most 16 tool calls per response")
    identifiers = set()
    for call in calls:
        if not isinstance(call, dict) or call.get("type") != "function":
            raise ValueError("expected function tool call")
        identifier = call.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("missing or duplicate tool call id")
        identifiers.add(identifier)
        function = call.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not isinstance(function.get("arguments"), str):
            raise ValueError("malformed function tool call")
    if not calls and not message.get("content"):
        raise ValueError("empty assistant response")

