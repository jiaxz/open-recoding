"""Receipt event ordering follows the committed inputs, without model/API IO."""

from copy import deepcopy
import json
from types import SimpleNamespace
import unittest

from workspace_support import temporary_workspace

from multiagent.storage import MemberStore
from multiagent.tools import HostTools
from multiagent.tools import agent_tools
from multiagent.worker import format_input_events
from session.store import SessionStore
from team_fixtures import fixture_team as MultiAgentSystem


def collaborator(identifier, content):
    envelope = {"id": identifier, "source": "sub", "target": "master", "kind": "message",
                "content": content, "timestamp": 10}
    return {"role": "user", "_agent_message_id": identifier,
            "content": content, "_agent_message": {key: value for key, value in envelope.items() if key != "content"}}


def human(identifier):
    return {"role": "user", "_agent_message_id": identifier, "content": [{"type": "text", "text": "检查数据"}],
            "_user_input": {"timestamp": 11, "attachments": [{"id": "snapshot", "name": "chart.png"}]}}


class MessageTimelineTests(unittest.TestCase):
    def test_mixed_user_and_collaborator_inputs_keep_context_order_and_source_times(self):
        messages = [collaborator("first", "found data"), human("second"),
                    collaborator("third", "checked sources"), collaborator("fourth", "written report")]
        original = deepcopy(messages)
        events, offset = format_input_events(messages, step=3, target="master", offset=0, received_at=25)
        self.assertEqual([event["type"] for event in events],
                         ["agent_messages", "user_input_applied", "agent_messages"])
        identifiers, orders = [], []
        for event in events:
            if event["type"] == "agent_messages":
                identifiers.extend(message["id"] for message in event["messages"])
                orders.extend(message["input_order"] for message in event["messages"])
            else:
                identifiers.append(event["message_id"])
                orders.append(event["input_order"])
        self.assertEqual(identifiers, ["first", "second", "third", "fourth"])
        self.assertEqual(orders, [0, 1, 2, 3])
        self.assertEqual(offset, 4)
        self.assertEqual(events[1]["target"], "master")
        self.assertEqual(events[1]["timestamp"], 11)
        self.assertEqual(events[1]["received_at"], 25)
        self.assertEqual(messages, original)
        # UI consumers may decorate events, but cannot modify the saved model history.
        events[1]["attachments"][0]["name"] = "changed"
        events[1]["content"][0]["text"] = "changed"
        self.assertEqual(messages, original)

    def test_multiple_receipts_for_one_call_continue_order_instead_of_restarting(self):
        guard = [collaborator("report", "finished"), human("followup")]
        first, offset = format_input_events(guard, step=4, target="sub", offset=0, received_at=20)
        drained = [human("extra"), collaborator("reply", "approved")]
        second, offset = format_input_events(drained, step=4, target="sub", offset=offset, received_at=21)
        self.assertEqual(first[0]["messages"][0]["input_order"], 0)
        self.assertEqual(first[1]["input_order"], 1)
        self.assertEqual(second[0]["input_order"], 2)
        self.assertEqual(second[1]["messages"][0]["input_order"], 3)
        self.assertEqual(offset, 4)
        self.assertTrue(all(event["step"] == 4 for event in [*first, *second]))
        self.assertEqual(second[0]["target"], "sub")
        self.assertEqual(second[0]["received_at"], 21)
        # A different model call starts with its own zero offset.
        next_call, offset = format_input_events([human("next-call")], step=5, target="sub", offset=0, received_at=30)
        self.assertEqual(next_call[0]["input_order"], 0)
        self.assertEqual(offset, 1)

    def test_agents_use_dedicated_human_output_instead_of_agent_recipient_channel(self):
        with temporary_workspace() as temporary:
            config = {"max_subagents": 1}
            store = SessionStore.create(temporary, "message boundary", config)
            events = []
            runtime = SimpleNamespace(status="running", closed=False, publish=events.append)
            team = MultiAgentSystem(store, config, lambda *_: self.fail("No worker should start"), events.append, runtime)
            MemberStore.create_agent(store, "a" * 32, "peer", config)
            team.data["agents"].append({"id": "a" * 32, "status": "running"})
            team.agents["a" * 32] = runtime
            team._save()
            original = team.path.read_bytes()
            for source in ("master", "a" * 32):
                executor = HostTools(lambda action, fields: team.request(source, action, fields),
                                             lambda: [], schemas=agent_tools(source))
                for target in ("user", "human"):
                    with self.subTest(source=source, target=target):
                        result = executor.execute("send_agent_message", json.dumps({"agent_id": target, "message": "hello"}))
                        self.assertIn("error", result)
                        self.assertEqual(team.data["messages"], [])
                        self.assertEqual(team.path.read_bytes(), original)
            for source in ("master", "a" * 32):
                executor = HostTools(lambda action, fields: team.request(source, action, fields),
                                             lambda: [], schemas=agent_tools(source))
                result = executor.execute("send_user_message", json.dumps({"message": "useful answer"}))
                self.assertEqual(result["target"], "user")
            self.assertEqual([event["source"] for event in events if event["type"] == "human_message"],
                             ["master", "a" * 32])
            self.assertEqual([m for m in team.data["messages"] if m["target"] != "user"], [])
            self.assertEqual([item["source"] for item in [m for m in team.data["messages"] if m["kind"] == "human_output"]], ["master", "a" * 32])
