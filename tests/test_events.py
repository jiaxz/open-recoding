"""Core event and API usage contracts, independent of presentation."""

import io
import json
import unittest
from unittest.mock import Mock

from multiagent.loop import AgentLoop as Agent
from multiagent.errors import AgentError
from multiagent.client import Client


def tool_call(identifier="call-1", name="python"):
    return {"id": identifier, "type": "function", "function": {
        "name": name, "arguments": json.dumps({"code": "print(42)"})}}


class EventTests(unittest.TestCase):
    def test_loop_events_match_real_execution_and_usage(self):
        client, sandbox = Mock(), Mock()
        client.last_usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        client.complete.side_effect = [
            {"role": "assistant", "content": "先执行并检查。", "reasoning_content": "检查输入并验证。",
             "tool_calls": [tool_call(), tool_call("call-2")]},
            {"role": "assistant", "content": "42"},
            KeyboardInterrupt(),
        ]
        sandbox.execute.side_effect = [ValueError("invalid arguments"), {"stdout": "42\n"}, {"reason": "timeout"}]
        events = []
        agent = Agent(client, sandbox, on_event=events.append)
        with self.assertRaises(KeyboardInterrupt):
            agent.run()
        ended = [event for event in events if event["type"] == "model_end"]
        self.assertEqual([event["content"] for event in ended], ["先执行并检查。", "42"])
        self.assertEqual(ended[0]["reasoning_content"], "检查输入并验证。")
        self.assertEqual(ended[0]["calls"], [tool_call(), tool_call("call-2")])
        self.assertEqual([event["usage"]["total_tokens"] for event in ended], [15, 15])
        tools = [event for event in events if event["type"] == "tool_end"]
        self.assertIn("invalid arguments", tools[0]["result"]["error"])
        self.assertEqual(tools[-1]["origin"], "system")
        self.assertEqual(sandbox.execute.call_count, 3)
        self.assertEqual(events[-1]["type"], "loop_end")
        self.assertEqual(events[-1]["tools"], 3)
        self.assertEqual(events[-1]["steps"], 3)
        self.assertFalse(any(event["type"].startswith("turn_") for event in events))

    def test_cancel_and_model_failures_preserve_committed_context(self):
        for scenario, failure in (("cancelled", KeyboardInterrupt()), ("error", AgentError("connection failed"))):
            with self.subTest(scenario=scenario):
                client, sandbox, events = Mock(), Mock(), []
                client.complete.side_effect = failure
                incoming = {"role": "user", "content": "queued task", "_agent_message_id": "execution"}
                agent = Agent(client, sandbox, input_provider=lambda _: [incoming], on_event=events.append)
                with self.assertRaises(type(failure)):
                    agent.run()
                error = next(event for event in events if event["type"] == "loop_error")
                self.assertEqual(error["reason"], scenario)
                self.assertTrue(error["context_retained"])
                self.assertEqual(agent.messages[1], incoming)
                self.assertTrue(agent.messages[-1]["content"].startswith("[Agent停止记录]"))
                self.assertEqual(error["location"]["phase"], "model")


    def test_client_reports_only_valid_usage_and_clears_stale_values(self):
        client = Client("test-key")
        client.opener = Mock()
        message = {"role": "assistant", "content": "done"}
        client.opener.open.side_effect = [
            io.BytesIO(json.dumps({"choices": [{"message": message}], "usage": {
                "prompt_tokens": 20, "completion_tokens": -1, "total_tokens": True, "unexpected": 200}}).encode()),
            io.BytesIO(json.dumps({"choices": [{"message": message}]}).encode()),
        ]
        client.complete([])
        self.assertEqual(client.last_usage, {"prompt_tokens": 20})
        client.complete([])
        self.assertEqual(client.last_usage, {})

