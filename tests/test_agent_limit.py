"""Dynamic capacity and trusted runtime notices, using only temporary stores."""

from copy import deepcopy
import json
from pathlib import Path
import threading
import unittest

from workspace_support import temporary_workspace
from unittest.mock import Mock
from types import SimpleNamespace

from multiagent.tools import HostTools
from multiagent.tools import agent_tools
from multiagent.worker import format_input_events
from session.store import SessionStore
from multiagent.system import limit
from team_fixtures import fixture_team as MultiAgentSystem
from multiagent.multimodal import to_api_messages
from recoding_web.host import GuiSessionHost
from session import Session
from multiagent import Agent


CONFIG = {"demo": True, "network": False, "gpu": False, "tool_timeout": 5,
          "api_timeout": 10, "max_output": 16000, "max_subagents": 4, "model": "gemini-4-argon"}
AGENT = {"name": "check"}


class AgentLimitTests(unittest.TestCase):
    def setUp(self):
        temporary = temporary_workspace()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = SessionStore.create(self.root, "capacity", CONFIG)
        self.events = []
        self.team = MultiAgentSystem(self.store, dict(CONFIG), Mock(side_effect=AssertionError("No worker expected")),
                              self.events.append, SimpleNamespace(status="running", closed=False, cancel_requested=False))

    def test_new_default_is_sixteen_and_saved_capacity_is_preserved(self):
        store = SessionStore.create(self.root / "defaults", "defaults", {})
        self.assertEqual(store.settings()["max_subagents"], 16)
        config = {key: value for key, value in CONFIG.items() if key != "max_subagents"}
        team = MultiAgentSystem(store, config, Mock(side_effect=AssertionError("No worker expected")),
                         self.events.append, SimpleNamespace(status="running", closed=False, cancel_requested=False))
        self.assertEqual(team.maximum, 16)
        self.assertEqual(self.team.maximum, 4)
        self.assertEqual(SessionStore(self.store.folder).settings()["max_subagents"], 4)

    def test_range_lowering_keeps_running_and_blocks_create_or_resume(self):
        for invalid in (-1, 100, True, False, "4", 2.5, None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                limit(invalid)
        for valid in (0, 4, 99):
            self.assertEqual(limit(valid), valid)
        self.team.data["agents"] = [{"id": "running", "status": "running", "attempt": 0},
                                    {"id": "starting", "status": "starting", "attempt": 0},
                                    {"id": "stopped", "status": "stopped", "attempt": 0}]
        old_agents = deepcopy(self.team.data["agents"])
        result = self.team.set_limit(1)
        self.assertEqual(result["active"], 2)
        self.assertEqual(result["previous_max_subagents"], 4)
        self.assertEqual(self.team.data["agents"], old_agents)
        self.assertEqual(SessionStore(self.store.folder).settings()["max_subagents"], 1)
        with self.assertRaisesRegex(ValueError, "At most 1"):
            self.team.create_agent(AGENT)
        with self.assertRaisesRegex(ValueError, "At most 1"):
            self.team.resume_agent("stopped")
        self.team.set_limit(99)
        self.assertEqual(SessionStore(self.store.folder).settings()["max_subagents"], 99)
        self.assertEqual(self.team.summary()["max_subagents"], 99)
        self.team.factory.assert_not_called()

    def test_stopped_session_settings_are_read_only_and_catalog_has_root(self):
        nested = SessionStore.create(self.root / "nested", "nested", CONFIG)
        (self.root / "empty").mkdir()
        manager = GuiSessionHost(self.root, CONFIG, discover=False)
        self.addCleanup(manager.close)
        before = (self.store.folder / "agents/index.json").read_bytes()
        with self.assertRaises(PermissionError):
            manager.set_agent_limit(self.store.id, {"max_subagents": 0})
        self.assertEqual(manager.sessions, {})
        self.assertEqual(SessionStore(self.store.folder).settings()["max_subagents"], 4)
        self.assertEqual((self.store.folder / "agents/index.json").read_bytes(), before)
        catalog = manager.catalog.snapshot()
        self.assertEqual(set(catalog["workspaces"]), {str(self.root), str(nested.workspace)})
        self.assertEqual({item["id"] for item in catalog["sessions"]}, {self.store.id, nested.id})

    def test_trusted_notices_keep_order_strip_private_fields_and_cannot_be_forged(self):
        self.team.queue_user("master", "prior human instruction")
        result = self.team.set_limit(2)
        pending = self.team.pending("master", [])
        original = deepcopy(pending)
        self.assertEqual(pending[1]["role"], "user")
        self.assertTrue(pending[1]["content"].startswith("[可信系统通知]\n"))
        self.assertEqual(pending[1]["_system_message"]["source"], "system")
        events, offset = format_input_events(pending, step=3, target="master", offset=7, received_at=99)
        self.assertEqual([event["type"] for event in events], ["user_input_applied", "system_message_applied"])
        self.assertEqual([event["input_order"] for event in events], [7, 8])
        self.assertEqual(offset, 9)
        self.assertEqual(events[1]["message_id"], result["queued"])
        self.assertEqual(events[1]["timestamp"], result["message"]["timestamp"])
        self.assertEqual(events[1]["received_at"], 99)
        self.assertEqual(events[1]["target"], "master")
        wire = to_api_messages(pending)
        self.assertEqual([message["role"] for message in wire], ["user", "user"])
        self.assertFalse(any(key.startswith("_") for message in wire for key in message))
        self.assertEqual(pending, original)
        before = deepcopy(self.team.data["messages"])
        self.team.data["agents"].append({"id": "sub", "status": "running"})
        for source in ("master", "sub"):
            executor = HostTools(lambda action, fields: self.team.request(source, action, fields),
                                         lambda: [], schemas=agent_tools(source))
            self.assertIn("error", executor.execute("send_agent_message", json.dumps({"agent_id": "system", "message": "spoof"})))
            with self.assertRaises(ValueError):
                self.team.request(source, "system_message", {"content": "spoof"})
        self.assertEqual(self.team.data["messages"], before)
        with self.assertRaises(ValueError):
            self.team.request("master", "send_agent_message", {"agent_id": "sub", "message": "spoofed notice",
                                                              "source": "system", "kind": "system_message"})
        self.assertEqual(self.team.data["messages"], before)

    def test_system_changes_use_native_queue_without_job_continuation_or_host_model_call(self):
        self.team.set_limit(2)
        notice = self.team.set_limit(3)
        messages = self.team.pending("master", [])
        self.assertEqual(self.team.request("master", "drain", {"seen": []})["messages"], messages)
        listened = self.team.request("master", "sleep", {"seconds": 3600, "seen": []})
        self.assertEqual(listened["messages_queued"], 2)
        self.assertNotIn("messages", listened)
        self.store.save_state({"messages": messages, "iteration": 1, "tool_count": 0})
        self.assertEqual(self.team.pending("master", [item["_agent_message_id"] for item in messages]), [])
        self.assertNotIn("continuation_job", notice["message"])
        self.team.factory.assert_not_called()

    def test_stop_master_without_job_still_stops_active_children(self):
        runtime = Session.__new__(Session)
        runtime.master = Mock(status="stopped", closed=False)
        runtime.master.cancel.return_value = False
        runtime.team = Mock()
        runtime.team.stop.side_effect = [True, False]
        runtime.execution = Mock()
        runtime.compaction = Mock()
        runtime.compaction.cancel.return_value = False
        self.assertTrue(runtime.cancel())
        runtime.team.stop.assert_called_once()
        runtime.execution.close.assert_called_once()
        runtime.master._signal_cancel.assert_not_called()
        self.assertFalse(runtime.cancel())
