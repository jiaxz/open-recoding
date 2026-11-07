"""A pure endless model-to-tools loop and its narrow contracts."""

import json
import math
import time
import uuid
from contextlib import AbstractContextManager, nullcontext
from copy import deepcopy
from typing import Any, Callable, Protocol

from .multimodal import context_stats, make_tool_message
from .prompts import system_prompt
from .protocol import readable_reasoning, validate_message


Message = dict[str, Any]
AgentEvent = dict[str, Any]
EventHandler = Callable[[AgentEvent], None]
ResponseProcessor = Callable[[Message, list[Message]], Message]
InputProvider = Callable[[list[Message]], list[Message]]
ContextProcessor = Callable[[list[Message]], list[Message]]


class ModelClient(Protocol):
    """Only complete() is required; usage/observer/configuration are optional."""

    def complete(self, messages: list[Message]) -> Message:
        """Return one complete assistant message or raise an error/interruption."""
        ...


class ToolExecutor(Protocol):
    """Only execute() is required; runtime reset/state capabilities are optional."""

    def execute(self, name: str, arguments: str) -> dict[str, Any]:
        """Execute a tool using its serialized arguments and return its result."""
        ...


class HistorySink(Protocol):
    def record(self, kind: str, **fields: Any) -> None:
        """Record a structured observation; the core owns no history file."""
        ...


class AgentLoop:
    def __init__(self, client: ModelClient, executor: ToolExecutor, *, system: str | None = None,
                 on_event: EventHandler | None = None, history: HistorySink | None = None,
                 response_processor: ResponseProcessor | None = None,
                 input_provider: InputProvider | None = None, idle_sleep_seconds: float = 10,
                 context_processor: ContextProcessor | None = None):
        self.client, self.executor = client, executor
        self._on_event = on_event or (lambda event: None)
        self.history, self.response_processor = history, response_processor
        self.input_provider = input_provider
        self.context_processor = context_processor
        self.idle_sleep_seconds = idle_sleep_seconds
        self.model_step = self.tool_count = 0
        self.system = system if system is not None else system_prompt(
            network=getattr(executor, "network", True) is not False)
        self.messages = [{"role": "system", "content": self.system}]
        observer = getattr(client, "set_observer", None)
        if callable(observer):
            observer(self._observe_model)
        configuration = getattr(client, "configuration", None)
        if self.history is not None and callable(configuration):
            value = configuration()
            if isinstance(value, dict):
                self._record("api_configuration", **value)

    def _emit(self, event):
        self._on_event(deepcopy(event))

    def _record(self, kind, **fields):
        if self.history is not None:
            self.history.record(kind, **deepcopy(fields))

    def _observe_model(self, observation):
        self._record("model_transport", step=self.model_step, **observation)
        self._emit({"type": "model_transport", "step": self.model_step, **observation})

    def _append(self, message):
        self.messages.append(deepcopy(message))
        self._record("message", index=len(self.messages) - 1, message=message,
                     iteration=self.model_step, tool_count=self.tool_count)

    def restore(self, messages, *, iteration=0, tool_count=0, runtime_note=None):
        """Restore committed messages; close interrupted calls without replaying them."""
        if (not isinstance(messages, list) or not messages or not isinstance(messages[0], dict) or messages[0].get("role") != "system"
                or not isinstance(messages[0].get("content"), str)):
            raise ValueError("saved context must start with a System message")
        if any(type(value) is not int or value < 0 for value in (iteration, tool_count)):
            raise ValueError("invalid saved Agent counters")
        pending = {}
        for message in messages[1:]:
            if not isinstance(message, dict) or message.get("role") not in {"user", "assistant", "tool"}:
                raise ValueError("invalid saved context message")
            if message["role"] == "tool":
                identifier = message.get("tool_call_id")
                if identifier not in pending or not isinstance(message.get("content"), str):
                    raise ValueError("saved tool result is not paired with its call")
                pending.pop(identifier)
            else:
                if pending:
                    raise ValueError("saved context contains an incomplete tool batch")
                if message["role"] == "assistant":
                    validate_message(message)
                    pending = {call["id"]: call for call in message.get("tool_calls") or []}
                elif not isinstance(message.get("content"), (str, list)):
                    raise ValueError("invalid saved user message")
        self.messages = deepcopy(messages)
        self.system = self.messages[0]["content"]
        self.model_step, self.tool_count = iteration, tool_count
        for identifier in pending:
            self._append({"role": "tool", "tool_call_id": identifier, "content": json.dumps({
                "status": "interrupted", "error": "运行环境已停止，此调用没有完整结果；不会自动重放，请先检查文件实际状态。"}, ensure_ascii=False)})
        self._record("context_resume", messages=self.messages, iteration=iteration, tool_count=tool_count,
                     previous_location=runtime_note, uncommitted_tool_calls=list(pending))

    def _close_pending(self, progress, reason, error):
        """Keep completed steps and make every remaining call/result pair valid."""
        pending = {}
        for message in self.messages:
            for call in message.get("tool_calls") or []:
                pending[call["id"]] = call
            if message["role"] == "tool":
                pending.pop(message["tool_call_id"], None)
        interrupted, not_started = [], []
        for identifier, call in pending.items():
            active = progress.get("phase") == "tool" and progress.get("call_id") == identifier
            status = "interrupted" if active else "not_started"
            self._append({"role": "tool", "tool_call_id": identifier, "content": json.dumps({
                "status": status, "error": "Agent 已停止，此操作未提交完整结果；文件可能已改变，恢复后先核验。"}, ensure_ascii=False)})
            (interrupted if active else not_started).append(identifier)
        record = {"reason": reason, "error": error, "iteration": self.model_step, "location": dict(progress),
                  "interrupted_tool_calls": interrupted, "not_started_tool_calls": not_started,
                  "note": "已提交的上下文与工具结果全部保留；文件修改未撤销，恢复不自动重放。"}
        self._append({"role": "user", "content": "[Agent停止记录]\n" + json.dumps(record, ensure_ascii=False)})
        return record

    def _fallback_sleep(self):
        seconds = self.idle_sleep_seconds
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 3600:
            raise ValueError("invalid default sleep duration")
        # A separate host-authored message preserves the model's original text,
        # reasoning and signature verbatim and supplies a valid tool/result pair.
        call = {"id": "system-sleep-" + uuid.uuid4().hex, "type": "function",
                "function": {"name": "sleep", "arguments": json.dumps({"seconds": seconds})}}
        self._append({"role": "assistant", "content": None, "tool_calls": [call],
                      "_system_generated": True, "_generated_reason": "model_returned_no_tool_calls"})
        return [call]

    def run(self, *, cancel_scope: AbstractContextManager | None = None):
        """Every tool batch leads to another model call, until Stop or failure."""
        started = time.monotonic()
        model_elapsed = tool_elapsed = 0.0
        initial_step, initial_tools = self.model_step, self.tool_count
        progress = {"phase": "starting", "step": self.model_step}
        reason, error = "stopped", None
        try:
            with cancel_scope if cancel_scope is not None else nullcontext():
                self._emit({"type": "loop_start", "step": self.model_step})
                while True:
                    self.model_step += 1
                    step = self.model_step
                    progress = {"phase": "model", "step": step}
                    # Derive the request history without altering the committed
                    # journal. Drain inboxes afterwards so preparation latency
                    # cannot strand messages until an extra model iteration.
                    prepared = self.context_processor(self.messages) if self.context_processor else None
                    # The sole mutation point for inbox inputs is just before
                    # the next model call, after the complete prior tool batch.
                    pending = self.input_provider(self.messages) if self.input_provider else []
                    for incoming in pending:
                        if not isinstance(incoming, dict) or incoming.get("role") != "user" or not isinstance(incoming.get("content"), str):
                            raise ValueError("pending input must be a user text message")
                        self._append(incoming)
                    if pending:
                        self._emit({"type": "inputs_received", "step": step, "messages": pending})
                    request_messages = [*prepared, *pending] if prepared is not None else self.messages
                    stats = context_stats(request_messages)
                    self._record("model_request", step=step, iteration=step, **stats)
                    self._emit({"type": "model_start", "step": step, **stats})
                    requested = time.monotonic()
                    try:
                        message = self.client.complete(request_messages)
                        validate_message(message)
                    finally:
                        elapsed = time.monotonic() - requested
                        model_elapsed += elapsed
                    source = getattr(self.client, "model", None)
                    if isinstance(source, str) and source:
                        message = {**message, "_model": source}
                    if self.response_processor:
                        message = self.response_processor(deepcopy(message), deepcopy(self.messages))
                        validate_message(message)
                    self._append(message)
                    calls = message.get("tool_calls") or []
                    usage = getattr(self.client, "last_usage", {})
                    progress = {"phase": "model_response", "step": step}
                    self._emit({"type": "model_end", "step": step, "elapsed": elapsed,
                                "tool_calls": len(calls), "usage": deepcopy(usage) if isinstance(usage, dict) else {},
                                "content": message.get("content"), "reasoning_content": readable_reasoning(message),
                                "calls": calls, "images": message.get("_display_images", []),
                                "image_errors": message.get("_display_image_errors", [])})
                    generated = not calls
                    if generated:
                        calls = self._fallback_sleep()
                    for index, call in enumerate(calls, 1):
                        self.tool_count += 1
                        function = call["function"]
                        fields = {"step": step, "number": self.tool_count, "index": index, "count": len(calls),
                                  "call_id": call["id"], "name": function["name"],
                                  "origin": "system" if generated else "model"}
                        progress = {"phase": "before_tool", **fields}
                        self._record("tool_start", **fields)
                        self._emit({"type": "tool_start", **fields, "arguments": function["arguments"]})
                        tool_started = time.monotonic()
                        progress = {"phase": "tool", **fields}
                        try:
                            try:
                                result = self.executor.execute(function["name"], function["arguments"])
                            except Exception as exc:
                                result = {"error": str(exc), "exit_code": 1}
                            tool_message, result = make_tool_message(self.messages, call["id"], result)
                            self._append(tool_message)
                        finally:
                            elapsed = time.monotonic() - tool_started
                            tool_elapsed += elapsed
                        progress = {"phase": "after_tool", **fields}
                        self._record("tool_end", **fields)
                        self._emit({"type": "tool_end", **fields, "result": result, "elapsed": elapsed})
                    self._emit({"type": "feedback", "step": step, "count": len(calls)})
        except BaseException as exc:
            reason = "cancelled" if isinstance(exc, KeyboardInterrupt) else "error"
            error = str(exc)
            stopped = self._close_pending(progress, reason, error)
            self._record("loop_error", reason=reason, error=error, location=progress,
                         iteration=self.model_step, tool_count=self.tool_count, messages=self.messages)
            self._emit({"type": "loop_error", "reason": reason, "error": error, "step": self.model_step,
                        "location": progress, "cancellation": stopped, "context_retained": True})
            raise
        finally:
            totals = {"reason": reason, "elapsed": time.monotonic() - started, "model_elapsed": model_elapsed,
                      "tool_elapsed": tool_elapsed, "steps": self.model_step - initial_step,
                      "tools": self.tool_count - initial_tools, "iteration": self.model_step, "tool_count": self.tool_count}
            self._record("loop_end", **totals)
            self._emit({"type": "loop_end", **totals})
