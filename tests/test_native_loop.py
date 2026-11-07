"""Endless model/tools ordering, host fallback, retained state and media boundaries."""

from copy import deepcopy
import json
import unittest

from workspace_support import temporary_workspace

from multiagent.loop import AgentLoop as Agent
from multiagent.models import model_messages
from session.store import AgentHistory, SessionStore
from multiagent.multimodal import make_user_message, to_api_messages
from test_images import png
from multiagent.image_reader import image_attachment


def tool(identifier, name, fields):
    return {"role": "assistant", "tool_calls": [{"id": identifier, "type": "function",
            "function": {"name": name, "arguments": json.dumps(fields)}}]}


class NativeLoopTests(unittest.TestCase):
    def test_tools_always_lead_to_another_model_call_and_plain_response_gets_sleep(self):
        timeline, requests, events, inbox = [], [], [], []
        outer = self

        class Model:
            model = "gemini-4-argon"
            def complete(self, messages):
                requests.append(deepcopy(messages))
                timeline.append("model")
                if len(requests) == 1:
                    return tool("work", "python", {"code": "print(42)"})
                if len(requests) == 2:
                    return {"role": "assistant", "content": "Original plain output", "extra_content": {"signature": "retain-me"}}
                return tool("idle", "sleep", {"seconds": 50})

        class Tools:
            def execute(self, name, arguments):
                timeline.append(name)
                if len(requests) == 3:
                    raise KeyboardInterrupt()
                if name == "python":
                    inbox.append({"role": "user", "content": "A queued direction", "_agent_message_id": "human-1"})
                    return {"stdout": "42"}
                outer.assertEqual(json.loads(arguments), {"seconds": 10})
                return {"reason": "timeout", "messages_queued": 0}

        def inputs(messages):
            pending = list(inbox)
            inbox.clear()
            return pending

        agent = Agent(Model(), Tools(), system="fixture", input_provider=inputs, on_event=events.append)
        with self.assertRaises(KeyboardInterrupt):
            agent.run()
        self.assertEqual(timeline, ["model", "python", "model", "sleep", "model", "sleep"])
        self.assertEqual(requests[1][-1]["_agent_message_id"], "human-1")
        original = next(m for m in requests[2] if m.get("content") == "Original plain output")
        self.assertEqual(original["extra_content"], {"signature": "retain-me"})
        self.assertNotIn("tool_calls", original)
        fallback = next(m for m in requests[2] if m.get("_system_generated"))
        self.assertEqual(fallback["tool_calls"][0]["function"]["name"], "sleep")
        self.assertEqual(requests[2][-1]["tool_call_id"], fallback["tool_calls"][0]["id"])
        self.assertEqual([e["origin"] for e in events if e["type"] == "tool_start"], ["model", "system", "model"])
        self.assertFalse(any(e["type"].startswith("turn_") for e in events))
        self.assertEqual(agent.model_step, 3)
        self.assertEqual(agent.tool_count, 3)
        self.assertIn("Agent停止记录", agent.messages[-1]["content"])
        self.assertTrue(all(not any(k.startswith("_") for k in m) for m in model_messages(requests[2], Model.model)))

    def test_multi_tool_batch_commits_before_queue_is_drained(self):
        inbox, received, work = [], [], []
        class Model:
            def complete(self, messages):
                received.append(deepcopy(messages))
                if len(received) > 1:
                    raise KeyboardInterrupt()
                first = tool("one", "python", {"code": "one"})
                first["tool_calls"].extend(tool("two", "python", {"code": "two"})["tool_calls"])
                return first
        class Tools:
            def execute(self, name, arguments):
                work.append(arguments)
                inbox.append({"role": "user", "content": "update", "_agent_message_id": str(len(work))})
                return {"stdout": str(len(work))}
        def inputs(messages):
            result = list(inbox)
            inbox.clear()
            return result
        agent = Agent(Model(), Tools(), input_provider=inputs)
        with self.assertRaises(KeyboardInterrupt):
            agent.run()
        self.assertEqual([m["role"] for m in received[1][-4:]], ["tool", "tool", "user", "user"])
        self.assertEqual([m["_agent_message_id"] for m in received[1][-2:]], ["1", "2"])

    def test_stop_and_resume_keep_completed_tools_and_never_replay_partial_batch(self):
        class Model:
            def complete(self, messages):
                response = tool("done", "python", {"code": "one"})
                response["tool_calls"].extend(tool("cut", "python", {"code": "two"})["tool_calls"])
                response["tool_calls"].extend(tool("future", "python", {"code": "three"})["tool_calls"])
                return response
        class Tools:
            calls = 0
            def execute(self, name, arguments):
                self.calls += 1
                if self.calls == 2:
                    raise KeyboardInterrupt()
                return {"stdout": "committed"}
        with temporary_workspace() as directory:
            store = SessionStore.create(directory, "endless", {})
            history = AgentHistory(store, system="fixture", tools=[], model="fixture")
            executor = Tools()
            agent = Agent(Model(), executor, system="fixture", history=history)
            try:
                with self.assertRaises(KeyboardInterrupt):
                    agent.run()
                results = [json.loads(m["content"]) for m in agent.messages if m["role"] == "tool"]
                self.assertEqual(results[0], {"stdout": "committed"})
                self.assertEqual([r.get("status") for r in results[1:]], ["interrupted", "not_started"])
                history.record("agent_stopped", reason="stopped")
                saved = store.load_state()
                self.assertEqual(saved["iteration"], 1)
                self.assertEqual(saved["tool_count"], 2)
                restored = Agent(Model(), executor, system="fixture")
                restored.restore(saved["messages"], iteration=saved["iteration"], tool_count=saved["tool_count"])
                self.assertEqual(executor.calls, 2)
                self.assertEqual(restored.messages, agent.messages)
                self.assertNotIn("turn_number", saved)
            finally:
                history.close()

    def test_only_user_paste_and_view_image_add_image_bytes(self):
        picture = image_attachment(png(), "/workspace/input.png")
        user = make_user_message([], "see this", [picture])
        display = {"role": "assistant", "content": "![figure](/workspace/figure.png)", "_display_images": [picture]}
        peer = {"role": "user", "content": "[Agent消息] image path only: /workspace/peer.png", "_message_images": [picture]}
        wire = to_api_messages([{"role": "system", "content": "fixture"}, user, display, peer])
        self.assertEqual(sum(part.get("type") == "image_url" for m in wire if isinstance(m.get("content"), list) for part in m["content"]), 1)
        self.assertNotIn("_display_images", json.dumps(wire))
        self.assertIsInstance(wire[-1]["content"], str)


if __name__ == "__main__":
    unittest.main()
