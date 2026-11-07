"""Focused gateway schema/Resume regression; no real API, key or Session."""

from copy import deepcopy
import io
import json
import unittest
from unittest.mock import Mock, patch

from multiagent.client import Client
from multiagent.tools import agent_tools
from multiagent.models import MODELS
from multiagent.loop import AgentLoop as Agent
from multiagent.errors import AgentError
from multiagent.prompts import DEFAULT_MODEL


class Stream(io.BytesIO):
    status = 200
    headers = {"content-type": "text/event-stream"}


def frame(value):
    return b"data: " + json.dumps(value).encode() + b"\n\n"


class GrokResumeTests(unittest.TestCase):
    def test_resumed_sub_uses_plain_object_schema_and_keeps_foreign_history_lossless(self):
        signature = {"google": {"thought_signature": "gemini-signature-fixture"}}
        history = [{"role": "system", "content": "Synthetic sub-agent"},
                   {"role": "user", "content": "Prior work"},
                   {"role": "assistant", "content": None, "_model": "gemini-4-argon", "tool_calls": [
                       {"id": "old", "type": "function", "extra_content": signature,
                        "function": {"name": "python", "arguments": '{"code":"print(1)"}'}}]},
                   {"role": "tool", "tool_call_id": "old", "content": '{"stdout":"1"}'}]
        original = deepcopy(history)
        client = Client("synthetic-key", model="grok-4.7", tools=agent_tools("a" * 32), max_retries=3)
        client.opener = Mock()

        def strict_gateway(request, **kwargs):
            wire = json.loads(request.data)
            self.assertEqual(wire["model"], "grok-4.7")
            for item in wire["tools"]:
                schema = item["function"]["parameters"]
                self.assertEqual(schema["type"], "object")
                self.assertFalse({"oneOf", "anyOf"} & schema.keys(), "Gateway rejects root unions")
            schema = next(item["function"]["parameters"] for item in wire["tools"] if item["function"]["name"] == "send_agent_message")
            self.assertIn("agent_id", schema["properties"])
            self.assertIn("agent_ids", schema["properties"])
            self.assertNotIn("gemini-signature-fixture", json.dumps(wire))
            self.assertEqual(wire["messages"][-1]["tool_call_id"], "old")
            return Stream(frame({"choices": [{"delta": {"content": "Resumed"}, "finish_reason": "stop"}]}) + b"data: [DONE]\n\n")

        client.opener.open.side_effect = strict_gateway
        agent = Agent(client, Mock())
        agent.restore(history, iteration=12, tool_count=7)
        self.assertEqual(client.complete(agent.messages)["content"], "Resumed")
        self.assertEqual(client.opener.open.call_count, 1)
        self.assertEqual(agent.messages, original)
        self.assertEqual(history, original)
        self.assertEqual(DEFAULT_MODEL, "grok-4.7")
        self.assertIn(DEFAULT_MODEL, MODELS)
        self.assertEqual(len(MODELS),len(set(MODELS)))

    def test_http_200_invalid_schema_is_not_retried(self):
        client = Client("synthetic-key", model="grok-4.7", tools=agent_tools("a" * 32), max_retries=3)
        client.opener = Mock()
        error = {"error": {"code": "invalid-argument", "message": "[invalid_client_tool_schema] send_agent_message: root oneOf union with a non-object branch"}}
        client.opener.open.side_effect = lambda *args, **kwargs: Stream(frame(error))
        with patch("multiagent.client.time.sleep") as pause, self.assertRaisesRegex(AgentError, "invalid_client_tool_schema"):
            client.complete([{"role": "system", "content": "Synthetic sub-agent"}])
        self.assertEqual(client.last_call["http_status"], 200)
        self.assertEqual(client.last_call["error_category"], "invalid_request")
        self.assertEqual(client.opener.open.call_count, 1)
        pause.assert_not_called()
