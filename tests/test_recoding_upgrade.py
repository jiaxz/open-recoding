from multiagent.agent import RuntimeState
"""Focused current-format, global model, cumulative totals and user-queue contracts."""

import json
from pathlib import Path
import shutil
import threading
import unittest

from workspace_support import temporary_workspace
from unittest.mock import Mock

from multiagent.storage import MemberStore
from session.store import FORMAT, VERSION, SESSION_HOME, SessionStore, UnsupportedSession, atomic_json
from team_fixtures import fixture_team as MultiAgentSystem
from multiagent.multimodal import to_api_messages
from recoding_web.host import GuiSessionHost
from session import Session
from multiagent import Agent
from test_images import png

CONFIG = {"demo": True, "network": False, "gpu": False, "tool_timeout": 5,
          "api_timeout": 10, "max_output": 16000, "max_subagents": 2, "model": "gemini-4-argon",
          "base_url": "https://fixture.test/v1"}


class Runtime(RuntimeState):
    def __init__(self, store, config):
        self.record = {}
        self.store, self.config = store, dict(config)
        self.condition, self.status, self.commands = threading.Condition(), "running", []
        self.closed = False
    def _write(self, value):
        self.commands.append(value)
    def close(self):
        self.status, self.closed = "closed", True
    def publish(self, event):
        pass


class RecodingUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_workspace()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SessionStore.create(self.root, "fixture", CONFIG)

    def team(self):
        master = Runtime(self.store, CONFIG)
        team = MultiAgentSystem(self.store, dict(CONFIG), lambda store, cfg, *_: Runtime(store, cfg), lambda event: None, master)
        self.addCleanup(team.pause)
        return team

    def create(self, team):
        result = team.request("master", "create_agent", {"name": "worker"})
        team.observe(result["agent_id"], {"type": "loop_started"})
        return result["agent_id"]

    def test_global_model_applies_to_live_new_and_resumed_workers(self):
        team = self.team()
        identifier = self.create(team)
        self.assertEqual(team.listing()[0]["model"], CONFIG["model"])
        team.set_model("grok-4.7")
        self.assertEqual(team.request(identifier, "drain", {"seen": []})["model"], "grok-4.7")
        self.assertEqual(team.agents[identifier].config["model"], "grok-4.7")
        self.assertEqual(team.agents[identifier].commands, [], "Model selection is read only at the next drain")
        self.assertEqual(team.master.config["model"], "grok-4.7")
        team.request("master", "stop_agent", {"agent_id": identifier, "reason": "fixture stop"})
        for thread in team.cleaners:
            thread.join(1)
        self.assertEqual(team.listing()[0]["status"], "stopped")
        team.resume_agent(identifier)
        self.assertEqual(team.agents[identifier].config["model"], "grok-4.7")
        self.assertEqual(team.agents[identifier].config["base_url"], CONFIG["base_url"])
        newer = self.create(team)
        self.assertEqual(team.agents[newer].config["model"], "grok-4.7")

    def test_user_queue_keeps_authority_images_and_exact_message_ids(self):
        team = self.team()
        identifier = self.create(team)
        store = MemberStore(self.store, identifier)
        uploaded = store.add_upload(png(), "clipboard.png")
        message = team.queue_user(identifier, "别磨蹭，先核对数据。", [uploaded["id"]])
        pending = team.pending(identifier, [])
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["content"], message["content"])
        self.assertEqual(pending[0]["_user_input"]["attachments"][0]["sha256"], uploaded["sha256"])
        self.assertEqual(team.pending(identifier, [message["id"]]), [])
        self.assertEqual(to_api_messages(pending), [{"role": "user", "content": "<user_query>别磨蹭，先核对数据。</user_query>"}])
        persisted = json.loads(team.path.read_text())["messages"][-1]
        self.assertEqual((persisted["source"], persisted["target"], persisted["kind"]), ("user", identifier, "user_input"))
        self.assertNotIn("data_url", json.dumps(persisted))
        with self.assertRaises(ValueError):
            team.request(identifier, "queue_user", {"message": "spoof"})

    def test_ready_sub_runtime_queues_user_input_without_assignment_or_direct_model_call(self):
        team = self.team()
        identifier = self.create(team)
        runtime = object.__new__(Agent)
        runtime.record = {}
        runtime.condition, runtime.status = threading.Condition(), "ready"
        runtime.team, runtime.agent_id, runtime.run_id, runtime.closed = team, identifier, None, False
        runtime.store = MemberStore(self.store, identifier)
        runtime.publish, runtime.start = Mock(), Mock()
        accepted = runtime.submit("additional user direction", [])
        self.assertTrue(accepted["queued"])
        runtime.start.assert_not_called()
        self.assertEqual(team.pending(identifier, [])[0]["content"], "additional user direction")
        self.assertIsNone(runtime.publish.call_args)
        self.assertEqual(team.data["messages"][-1]["id"], accepted["queued"])
        self.assertTrue(team.data["messages"][-1]["published"])

    def test_totals_include_global_iterations_agents_and_resume_without_reset(self):
        team = self.team()
        identifier = self.create(team)
        for source, run_id, steps in (("master", "old-master", range(1, 3)), ("master", "new-master", range(3, 6)), (identifier, "sub-first", range(1, 5))):
            for step in steps:
                team.observe(source, {"type": "model_start", "run_id": run_id, "step": step})
                team.observe(source, {"type": "model_end", "run_id": run_id, "step": step, "elapsed": .1,
                                     "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}})
        before = team.summary()
        self.assertEqual((before["scope"], before["iterations"], before["model_calls"], before["prompt_tokens"]), ("session_lifetime", 9, 9, 90))
        self.assertNotIn("turns", before)
        team.pause()
        restored = MultiAgentSystem(self.store, dict(CONFIG), lambda *_: self.fail("No automatic peer resume"), lambda event: None, None)
        after = restored.summary()
        for key in ("model_calls", "iterations", "model_elapsed", "prompt_tokens", "completion_tokens"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(set(restored.agents), {"master"})
        self.assertEqual(after["stopped"], 1)
        self.assertNotIn("completed", after)

    def test_workspace_change_filters_navigation_and_preserves_background_runtime(self):
        other = self.root / "other"
        different = SessionStore.create(other, "other fixture", CONFIG)
        manager = GuiSessionHost(self.root, CONFIG)
        original_runtime = Runtime(self.store, CONFIG)
        manager.sessions[self.store.id] = original_runtime
        result = manager.catalog.change_workspace(str(other))
        self.assertEqual([item["id"] for item in result["sessions"]], [different.id])
        self.assertIs(manager.get(self.store.id), original_runtime)
        self.assertEqual(original_runtime.status, "running")
        self.assertEqual(original_runtime.store.workspace, self.root)
        self.assertEqual(manager.catalog.root, other)

    def test_v5_store_move_preserves_images_and_context_while_v4_is_hidden_unchanged(self):
        uploaded = self.store.add_upload(png(), "input.png")
        messages = [{"role": "system", "content": "original system"}]
        self.store.save_state({"messages": messages, "iteration": 0, "tool_count": 0})
        child = MemberStore.create_agent(self.store, "a" * 32, "child", CONFIG)
        child.save_state({"messages": messages})
        payload = (self.store.folder / "state.json").read_bytes()
        destination_root = self.root / "moved"
        destination = destination_root / SESSION_HOME / self.store.folder.name
        destination.parent.mkdir(parents=True)
        shutil.move(self.store.folder, destination)
        moved = SessionStore(destination)
        self.assertEqual((moved.folder / "state.json").read_bytes(), payload)
        self.assertEqual(moved.id, self.store.id)
        self.assertEqual(moved.workspace, destination_root)
        self.assertEqual(moved.metadata["format"], FORMAT)
        self.assertEqual(moved.metadata["version"], 5)
        self.assertEqual(moved.image_asset(uploaded["sha256"])[0], png())
        self.assertEqual(MemberStore(moved, "a" * 32).load_state()["messages"], messages)
        unsupported = SessionStore.create(destination_root, "old v4", CONFIG)
        atomic_json(unsupported.folder / "session.json", {**unsupported.metadata, "version": VERSION - 1})
        old_bytes = (unsupported.folder / "session.json").read_bytes()
        with self.assertRaises(UnsupportedSession):
            SessionStore(unsupported.folder)
        catalog = GuiSessionHost(destination_root, CONFIG).catalog.snapshot()
        self.assertEqual([row["id"] for row in catalog["sessions"]], [moved.id])
        self.assertEqual((unsupported.folder / "session.json").read_bytes(), old_bytes)
