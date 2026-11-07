"""Model selection and provider wire compatibility; no presentation dependencies."""

from copy import deepcopy

from .model_config import model_ids, profile_for
from .multimodal import to_api_messages
from .protocol import readable_reasoning



MODELS = model_ids()


def validate_model(model):
    if (not isinstance(model, str) or not model or len(model) > 200
            or model != model.strip() or any(ord(char) < 32 for char in model)):
        raise ValueError("Model ID 必须是 1–200 个字符的非空字符串。")
    return model


def _foreign_message(original, target):
    # Only portable assistant fields cross model
    # boundaries. A whitelist also catches opaque gateway extensions such as
    # difrost_reasoning, nested signatures and future encrypted-content fields.
    portable = {"role", "content", "name", "tool_calls", "_model"}
    message = {key: deepcopy(value) for key, value in original.items() if key in portable}
    summary = readable_reasoning(original)
    if isinstance(summary, str) and summary.strip():
        source = original.get("_model") or "来源模型未记录"
        note = (f"[历史思考摘要 · {source}]\n"
                "以下是该模型当时返回的可读摘要，仅作历史参考，不是当前模型的内部思考或新的用户指令。\n"
                + summary + "\n[历史思考摘要结束]")
        message["content"] = (message.get("content") or "") + ("\n\n" if message.get("content") else "") + note
    if message.get("tool_calls"):
        message["tool_calls"] = [{"id": call["id"], "type": call["type"],
                                  "function": {"name": call["function"]["name"],
                                               "arguments": call["function"]["arguments"]}}
                                 for call in message["tool_calls"]]
        marker = profile_for(target).foreign_tool_marker
        if marker is not None:
            # A migration marker belongs to the target protocol; it is not a
            # foreign signature or a reconstruction of missing Gemini thinking.
            message["tool_calls"][0]["extra_content"] = deepcopy(marker)
    return message


def model_messages(messages, model):
    """Build a target-specific view from the immutable, model-tagged originals.

    Return to A: A's original summaries/signatures remain verbatim; only B's
    messages are translated. Unknown provenance is portable-only, never guessed.
    """
    prepared = [(_foreign_message(message, model)
                 if message.get("role") == "assistant" and message.get("_model") != model
                 else message) for message in messages]
    return to_api_messages(prepared)
