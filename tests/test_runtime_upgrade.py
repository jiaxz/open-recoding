"""Focused agent retry and strict current-format persistence checks; no network."""

import io
import json
from pathlib import Path
import unittest

from workspace_support import temporary_workspace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from multiagent.client import Client
from multiagent import worker as runner
from multiagent.runtime_build import RUNTIME_REVISION
from multiagent.tools import AGENT_TOOLS, SUB_AGENT_TOOLS, OBSERVATION_TOOLS
from multiagent.prompts import TEAM_SYSTEM, SUB_SYSTEM
from session.store import FORMAT, VERSION, SessionStore, UnsupportedSession, atomic_json
from multiagent.loop import AgentLoop as Agent
from multiagent.errors import AgentError
from recoding_web.host import GuiSessionHost


def response(message):
    value = Mock()
    value.__enter__ = Mock(return_value=value)
    value.__exit__ = Mock(return_value=False)
    value.status, value.headers = 200, {"content-type": "application/json"}
    value.read.return_value = json.dumps({"choices": [{"message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}}).encode()
    return value


def unavailable(status=503):
    return HTTPError("https://fixture.test/v1/chat/completions", status, "fixture", {}, io.BytesIO(b'{"error":"fixture"}'))


class RuntimeUpgradeTests(unittest.TestCase):
    def test_master_and_sub_workers_enable_three_retries(self):
        with temporary_workspace() as directory:
            root = Path(directory)
            config = {"session_directory": str(root / "archive"), "directory": str(root),
                      "network": False, "demo": False, "api_timeout": 120,
                      "runtime_revision": RUNTIME_REVISION, "store_metadata": {}}
            for agent_id in ("master", "a" * 32):
                environment_reply = Mock()
                environment_reply.get.return_value = {"network": False, "gpu": {}}
                with self.subTest(agent_id=agent_id),\
                     patch.object(runner.signal, "signal"),\
                     patch.object(runner.sys, "stdin", io.StringIO(json.dumps({**config, "agent_id": agent_id}) + "\n")),\
                     patch.dict(runner.os.environ, {"RECODING_API_KEY": "fixture-secret"}, clear=True),\
                     patch.object(runner, "AgentStore") as store,\
                     patch.object(runner, "AgentHistory"),\
                     patch.object(runner.threading, "Thread"),\
                     patch.object(runner, "Queue", side_effect=[Mock(), environment_reply]),\
                     patch.object(runner, "compose_system", return_value="fixture system"),\
                     patch.object(runner, "send"),\
                     patch.object(runner, "Client", side_effect=AgentError("stop before worker loop")) as client:
                    store.return_value.folder = root / "archive"
                    store.return_value.load_state.return_value = None
                    runner.main()
                    self.assertEqual(client.call_count, 1)
                    self.assertEqual(client.call_args.kwargs["max_retries"], 3)

    def test_retries_same_context_without_replaying_completed_tools(self):
        client = Client("fixture-secret", max_retries=3)
        call = {"role": "assistant", "content": None, "tool_calls": [{"id": "one", "type": "function",
            "function": {"name": "python", "arguments": '{"code":"print(42)"}'}}]}
        client.opener = Mock()
        pause = {"role": "assistant", "content": "42", "tool_calls": [{"id": "pause", "type": "function",
                 "function": {"name": "sleep", "arguments": '{"seconds":0}'}}]}
        client.opener.open.side_effect = [response(call), unavailable(), response(pause)]
        executor, events = Mock(), []
        executor.execute.side_effect = [{"stdout": "42", "exit_code": 0}, KeyboardInterrupt]
        agent = Agent(client, executor, system="fixture", on_event=events.append)
        with patch("multiagent.client.time.sleep") as sleep, self.assertRaises(KeyboardInterrupt):
            agent.run()
        sleep.assert_called_once_with(1)
        self.assertEqual(executor.execute.call_args_list[0].args, ("python", '{"code":"print(42)"}'))
        self.assertEqual([call.args[0] for call in executor.execute.call_args_list], ["python", "sleep"])
        requests = [args.args[0].data for args in client.opener.open.call_args_list]
        self.assertEqual(requests[1], requests[2])
        self.assertEqual(sum(message.get("tool_call_id") == "one" for message in agent.messages), 1)
        self.assertIn("42", next(message["content"] for message in agent.messages if message.get("tool_call_id") == "one"))
        self.assertEqual(client.last_call["attempt"], 2)
        self.assertEqual(client.last_call["retry_count"], 1)
        self.assertEqual(client.last_call["failed_attempts"][0]["http_status"], 503)
        self.assertTrue(any(event.get("phase") == "retry_wait" for event in events))
        self.assertEqual(sum(event["type"] == "model_start" for event in events), 2)

    def test_retry_bounds_nonrecoverable_errors_and_cancellation(self):
        for status, attempts in ((400, 1), (401, 1), (403, 1), (429, 4), (503, 4)):
            with self.subTest(status=status):
                client = Client("fixture-secret", max_retries=3)
                client.opener = Mock()
                client.opener.open.side_effect = [unavailable(status) for _ in range(4)]
                with patch("multiagent.client.time.sleep"), self.assertRaises(AgentError):
                    client.complete([{"role": "user", "content": "fixture"}])
                self.assertEqual(client.opener.open.call_count, attempts)
        client = Client("fixture-secret", max_retries=3)
        client.opener = Mock()
        client.opener.open.side_effect = unavailable()
        executor = Mock()
        agent = Agent(client, executor, system="fixture")
        with patch("multiagent.client.time.sleep", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            agent.run()
        self.assertEqual(client.last_call["phase"], "cancelled")
        self.assertEqual(client.opener.open.call_count, 1)
        executor.execute.assert_not_called()
        self.assertIn("Agent停止记录", agent.messages[-1]["content"])

    def test_old_formats_hidden_unchanged_and_current_resume_roundtrip(self):
        with temporary_workspace() as temp:
            root = Path(temp)
            current = SessionStore.create(root, "current", {})
            current.save_state({"messages": [{"role": "system", "content": "fixture"}], "iteration": 0, "tool_count": 0})
            old = SessionStore.create(root, "unsupported", {})
            metadata = {**old.metadata, "version": VERSION - 1}
            atomic_json(old.folder / "session.json", metadata)
            original = (old.folder / "session.json").read_bytes()
            nested = old.folder / "agents" / "nested-current"
            nested.mkdir(parents=True)
            atomic_json(nested / "session.json", current.metadata)
            (root / "context_20261020_120000.jsonl").write_text('{"type":"session_start"}\n')
            manager = GuiSessionHost(root, {}, discover=False)
            manager.catalog.remember({"version": VERSION, "locations": [str(old.folder), str(current.folder)], "entries": {}})
            listing = manager.catalog.snapshot()
            self.assertEqual([row["id"] for row in listing["sessions"]], [current.id])
            self.assertEqual(listing["scan_errors"], [])
            with self.assertRaises(UnsupportedSession):
                SessionStore(old.folder)
            self.assertEqual((old.folder / "session.json").read_bytes(), original)
            self.assertEqual(SessionStore(current.folder).load_state()["messages"], [{"role": "system", "content": "fixture"}])
            self.assertEqual(current.metadata["format"], FORMAT)
            self.assertEqual(current.metadata["version"], VERSION)
            # Changing an already cached archive to an unsupported version hides it too.
            atomic_json(current.folder / "session.json", {**current.metadata, "version": VERSION + 1})
            self.assertEqual(manager.catalog.snapshot()["sessions"], [])

    def test_master_protocol_and_role_boundaries(self):
        self.assertIn("MASTER", TEAM_SYSTEM)
        self.assertIn("SUB Agent", SUB_SYSTEM)
        common = {tool["function"]["name"] for tool in SUB_AGENT_TOOLS}
        self.assertEqual(common, {"send_agent_message", "broadcast_agent_message", "send_user_message", "list_agents", "sleep"})
        names = {tool["function"]["name"] for tool in AGENT_TOOLS}
        self.assertEqual(names - common, {"create_agent", "stop_agent", "resume_agent", "rename_agent", "set_agent_icon"}
                         | {item["function"]["name"] for item in OBSERVATION_TOOLS})
        target = next(tool for tool in SUB_AGENT_TOOLS if tool["function"]["name"] == "send_agent_message")["function"]["parameters"]["properties"]["agent_id"]
        self.assertNotIn("enum", target)
        self.assertTrue(names.isdisjoint({"spawn_agent", "wait_agent", "wait_agents", "terminate_agent", "listen_messages"}))
