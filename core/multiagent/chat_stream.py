"""Bounded Chat Completions SSE reader; never exposes a partial tool call."""

import json
import time
from copy import deepcopy

from .protocol import REASONING_FIELDS


MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class StreamError(RuntimeError):
    def __init__(self, message, category="invalid_response", **metadata):
        super().__init__(message)
        self.category, self.metadata = category, metadata


def merge_fields(target, source):
    """Preserve vendor metadata (including Gemini thought signatures)."""
    for key, value in source.items():
        # Optional delta fields can be explicitly null on later chunks.
        # They mean no update, rather than clearing an earlier role/id/signature.
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_fields(target[key], value)
        else:
            target[key] = deepcopy(value)


def read_chat_stream(response, *, started, timeout, observe, redact,
                     max_bytes=MAX_RESPONSE_BYTES):
    message = {"role": "assistant", "content": None}
    text, tools, arguments = [], {}, {}
    reasoning = {key: [] for key in REASONING_FIELDS}
    result, data_lines = {}, []
    stats = {"response_bytes": 0, "stream_events": 0, "heartbeat_count": 0,
             "model_output_received": False, "sse_done_received": False}
    last_output = started
    last_emit = started
    finished = False

    def progress(*, first=False, force=False):
        nonlocal last_emit
        now = time.monotonic()
        if first or force or now - last_emit >= 5:
            observe(dict(stats), first)
            last_emit = now

    def event(payload):
        nonlocal finished, last_output
        if payload.strip() == "[DONE]":
            finished = True
            stats["sse_done_received"] = True
            return
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError("expected SSE JSON object")
        stats["stream_events"] += 1
        if value.get("error"):
            error = value["error"]
            detail = error if isinstance(error, dict) else {"message": str(error)}
            code = detail.get("code")
            category = "upstream_credentials" if code == "credential_unavailable" else "upstream_error"
            invalid = {"invalid-argument", "invalid_argument", "invalid_request", "invalid_request_error",
                       "invalid_client_tool_schema", "validation_error", "bad_request"}
            if any(str(detail.get(key, "")).lower() in invalid for key in ("code", "type")):
                category = "invalid_request"
            fields = {"upstream_" + key: redact(str(detail[key]))[:1500]
                      for key in ("type", "code", "message") if key in detail}
            raise StreamError("API stream error" + (f" [{code}]" if code else "")
                              + ": " + redact(str(detail.get("message", error))), category,
                              error_body=redact(json.dumps(value, ensure_ascii=False))[:2000], **fields)
        for key in ("model", "usage"):
            if value.get(key) is not None:
                result[key] = value[key]
        for choice in value.get("choices") or []:
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or {}
            if delta.get("role") not in {None, "", "assistant"}:
                raise ValueError("expected assistant stream role")
            meaningful = (bool(delta.get("content") or delta.get("tool_calls")) or
                          any(isinstance(delta.get(key), str) and delta[key] for key in REASONING_FIELDS))
            if meaningful:
                stats["model_output_received"] = True
                last_output = time.monotonic()
                first = "first_output_seconds" not in stats
                stats.setdefault("first_output_seconds", round(last_output - started, 3))
                progress(first=first)
            for key, parts in (("content", text), *reasoning.items()):
                value = delta.get(key)
                if value is not None:
                    if not isinstance(value, str):
                        if key in {"content", "reasoning_content"}:
                            raise ValueError("expected string " + key + " delta")
                        continue  # Preserve structured/opaque extensions without displaying them.
                    parts.append(value)
                    if key == "content" and value:
                        stats.setdefault("first_text_seconds", round(time.monotonic() - started, 3))
            merge_fields(message, {k: v for k, v in delta.items()
                                   if k not in {"role", "content", "reasoning_content", "tool_calls"}
                                   and not (k in REASONING_FIELDS and isinstance(v, str))})
            for call in delta.get("tool_calls") or []:
                index = call.get("index")
                if type(index) is not int or not 0 <= index < 16:
                    raise ValueError("expected tool call index from 0 to 15")
                target = tools.setdefault(index, {"type": "function", "function": {}})
                merge_fields(target, {k: v for k, v in call.items()
                                      if k not in {"index", "function"} and v not in (None, "")})
                function = call.get("function") or {}
                if function.get("name"):
                    old = target["function"].get("name", "")
                    target["function"]["name"] = old if old == function["name"] else old + function["name"]
                if function.get("arguments") is not None:
                    if not isinstance(function["arguments"], str):
                        raise ValueError("expected string tool arguments delta")
                    arguments.setdefault(index, []).append(function["arguments"])
                merge_fields(target["function"], {k: v for k, v in function.items() if k not in {"name", "arguments"}})
            if choice.get("finish_reason"):
                result["finish_reason"] = choice["finish_reason"]

    while not finished:
        if time.monotonic() - last_output > timeout:
            raise TimeoutError(f"API stream had no model output for {timeout:g}s (heartbeats do not count)")
        raw = response.readline(max_bytes - stats["response_bytes"] + 1)
        if not raw:
            # The periodic observer may lag the last few seconds of the stream.
            # Flush exact counters, without exposing any uncommitted text/code.
            progress(force=True)
            metadata = {**stats, "pending_event_bytes": sum(len(line.encode("utf-8")) for line in data_lines),
                        "response_model": result.get("model"), "finish_reason": result.get("finish_reason")}
            if not stats["model_output_received"] and not data_lines:
                raise StreamError(
                    "API gateway closed the stream before any model output "
                    f"({stats['heartbeat_count']} heartbeats, {stats['stream_events']} SSE data events; no [DONE]); "
                    "no tool call was committed. The gateway did not return an upstream error reason.",
                    "empty_response", **metadata)
            raise StreamError("API response interrupted: stream ended before [DONE]; no partial tool call was committed",
                              "incomplete_response", **metadata)
        stats["response_bytes"] += len(raw)
        if stats["response_bytes"] > max_bytes:
            raise StreamError("API response exceeds 4 MiB")
        stats.setdefault("first_event_seconds", round(time.monotonic() - started, 3))
        line = raw.rstrip(b"\r\n")
        if not line:
            if data_lines:
                event("\n".join(data_lines))
                data_lines.clear()
        elif line.startswith(b":"):
            stats["heartbeat_count"] += 1
        elif line.startswith(b"data:"):
            data = line[5:]
            data_lines.append((data[1:] if data.startswith(b" ") else data).decode("utf-8"))
        progress()
    message["content"] = "".join(text) if text else None
    for key, parts in reasoning.items():
        if parts:
            message[key] = "".join(parts)
    if tools:
        message["tool_calls"] = []
        for index, call in sorted(tools.items()):
            call["function"]["arguments"] = "".join(arguments.get(index, []))
            message["tool_calls"].append(call)
    progress(force=True)
    return {**{k: v for k, v in result.items() if k != "finish_reason"},
            "choices": [{"message": message, "finish_reason": result.get("finish_reason")}]}
