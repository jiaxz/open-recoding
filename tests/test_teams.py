from multiagent.agent import RuntimeState
"""Focused v5 queue/sleep/image/human-outbox and Stop concurrency checks."""

from copy import deepcopy
import json
from pathlib import Path
import threading
import time
import unittest

from workspace_support import temporary_workspace
from unittest.mock import Mock

from multiagent.storage import MemberStore
from multiagent.tools import HostTools
from multiagent.tools import agent_tools
from session.store import AgentHistory, SessionStore, UnsupportedSession, VERSION, atomic_json
from team_fixtures import fixture_team as MultiAgentSystem
from multiagent.image_reader import read_image
from multiagent.multimodal import image_usage, make_tool_message, to_api_messages
from test_images import png

CONFIG = {"demo": True, "network": False, "gpu": False, "tool_timeout": 5,
          "api_timeout": 10, "max_output": 16000, "max_subagents": 4, "model": "gemini-4-argon"}


class Runtime(RuntimeState):
    def __init__(self, store, config):
        self.record = {}
        self.store, self.config = store, dict(config)
        self.status, self.run_id, self.closed = "running", "native-fixture", False
        self.events, self.sequence = [], 0
    def close(self):
        self.closed, self.status = True, "closed"
    def publish(self, event):
        self.sequence += 1
        value = {**event, "sequence": self.sequence}
        self.events.append(value)
        self.store.append_event(value)
    def _write(self, command):
        self.command = command


class TeamTests(unittest.TestCase):
    def setUp(self):
        temporary = temporary_workspace()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = SessionStore.create(self.root, "v5", CONFIG)
        self.runtimes, self.events = [], []
        def factory(store, config, *_):
            runtime = Runtime(store, config)
            self.runtimes.append(runtime)
            return runtime
        self.factory = Mock(side_effect=factory)
        self.master = Runtime(self.store, CONFIG)
        self.team = MultiAgentSystem(self.store, dict(CONFIG), self.factory, self.events.append, self.master)
        self.addCleanup(self.team.pause)

    def create(self, name):
        identifier = self.team.request("master", "create_agent", {"name": name})["agent_id"]
        self.team.observe(identifier, {"type": "loop_started"})
        return identifier

    def test_severe_inventory_requests_host_setup_once_and_ignores_probe_failures(self):
        from node.inventory import evaluate
        from test_tool_inventory import discovery
        report = evaluate(discovery())
        self.team.observe("master", {"type": "tool_inventory", "inventory": report})
        notices = [message for message in self.team.data["messages"] if message.get("configuration", {}).get("event") == "host_tools_incomplete"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["source"], "system")
        self.assertEqual(notices[0]["target"], "master")
        self.assertIn("Host", notices[0]["content"])
        self.assertIn("/usr/bin/python3", notices[0]["content"])
        self.team.observe("master", {"type": "tool_inventory", "inventory": {**report, "checked_at": time.time()}})
        self.team.observe("master", {"type": "tool_inventory", "inventory": {"status": "error", "error": "timeout"}})
        self.team.observe("other", {"type": "tool_inventory", "inventory": report})
        self.assertEqual(len(self.team.data["messages"]), 1, "No repeat notices from telemetry or peer workers")
        self.team.observe("master", {"type": "tool_inventory", "inventory": evaluate(discovery(True))})
        self.assertEqual(len(self.team.data["messages"]), 1)
        self.team.observe("master", {"type": "tool_inventory", "inventory": report})
        self.assertEqual(len(self.team.data["messages"]), 2, "New degradation after recovery can notify again")

    def test_master_renames_live_and_stopped_sub_without_replacing_identity_or_queue(self):
        identifier = self.create("original")
        runtime = self.team.agents[identifier]
        self.team.queue_user(identifier, "Retain this unread input")
        before = deepcopy(self.team.data["messages"])
        result = self.team.request("master", "rename_agent", {"agent_id": identifier, "name": "  renamed  "})
        self.assertEqual(result, {"agent_id": identifier, "name": "renamed"})
        self.assertIs(self.team.agents[identifier], runtime)
        self.assertEqual(runtime.name, "renamed")
        self.assertEqual(MemberStore(self.store, identifier).name, "renamed")
        self.assertEqual(self.team.data["messages"], before)
        self.assertEqual(self.team.public_agents()[1]["name"], "renamed")
        with self.assertRaises(ValueError):
            self.team.request(identifier, "rename_agent", {"agent_id": identifier, "name": "denied"})
        tool = HostTools(Mock(), lambda: [], schemas=agent_tools(identifier))
        self.assertIn("error", tool.execute("rename_agent", json.dumps({"agent_id": identifier, "name": "denied"})))
        tool.rpc.assert_not_called()
        self.team.finish(identifier, "stopped", "pause")
        for cleaner in self.team.cleaners:
            cleaner.join(1)
        self.team.request("master", "rename_agent", {"agent_id": identifier, "name": "paused role"})
        self.assertEqual(MemberStore(self.store, identifier).name, "paused role")
        self.assertEqual(self.team.data["messages"], before)

    def test_master_chooses_and_changes_icons_without_replacing_agent_or_mail(self):
        identifier = self.team.request("master", "create_agent", {"name": "Analyst", "icon": "👩🏽‍💻"})["agent_id"]
        runtime = self.team.agents[identifier]
        self.team.queue_user(identifier, "Preserve unread input")
        messages = deepcopy(self.team.data["messages"])
        self.assertEqual(self.team.listing()[0]["icon"], "👩🏽‍💻")
        self.assertEqual(MemberStore(self.store, identifier).metadata["icon"], "👩🏽‍💻")
        self.team.request("master", "set_agent_icon", {"agent_id": identifier, "icon": "🔬"})
        self.assertIs(self.team.agents[identifier], runtime)
        self.assertEqual(runtime.store.metadata["icon"], "🔬")
        self.assertEqual(self.team.data["messages"], messages)
        self.team.request("master", "set_agent_icon", {"agent_id": "master", "icon": "♾️"})
        self.assertEqual(self.team.public_agents()[0]["icon"], "♾️")
        self.assertEqual(self.events[-1]["master_icon"], "♾️")
        self.assertEqual(self.events[-1]["agents"][0]["icon"], "🔬")
        self.team.finish(identifier, "stopped", "pause")
        for cleaner in self.team.cleaners:
            cleaner.join(1)
        self.team.request("master", "set_agent_icon", {"agent_id": identifier, "icon": "🎨"})
        restored = MultiAgentSystem(self.store, dict(CONFIG), self.factory, self.events.append, self.master)
        self.addCleanup(restored.pause)
        self.assertEqual(restored.master_icon, "♾️")
        self.assertEqual(restored.public_agents()[1]["icon"], "🎨")
        restored.request("master", "resume_agent", {"agent_id": identifier})
        self.assertEqual(restored.agents[identifier].store.metadata["icon"], "🎨")

    def test_icon_validation_and_master_only_permissions(self):
        from multiagent.config import validate_icon
        for icon in ("🔬", "👩🏽‍💻", "🏳️‍🌈", "🇨🇳", "1️⃣", "♾️"):
            self.assertEqual(validate_icon(icon), icon)
        for icon in (None, [], "", "too long", "<svg>", "https://example.org/icon.svg", "📊\n", "\u200d", "x", "🔬" * 17, "\ud800"):
            with self.subTest(icon=repr(icon)), self.assertRaises(ValueError):
                self.team.request("master", "create_agent", {"name": "Invalid", "icon": icon})
        self.factory.assert_not_called()
        identifier = self.create("Worker")
        for target in (identifier, "master"):
            with self.assertRaises(ValueError):
                self.team.request(identifier, "set_agent_icon", {"agent_id": target, "icon": "⚡"})
        tool = HostTools(Mock(), lambda: [], schemas=agent_tools(identifier))
        self.assertIn("error", tool.execute("set_agent_icon", json.dumps({"agent_id": identifier, "icon": "⚡"})))
        tool.rpc.assert_not_called()
        self.assertEqual(self.team.listing()[0]["icon"], "🤖")
        with self.assertRaises(KeyError):
            self.team.request("master", "set_agent_icon", {"agent_id": "unknown", "icon": "⚡"})

    def test_distinct_icons_are_prompt_guidance_not_a_backend_constraint(self):
        first = self.create("First")
        second = self.create("Second")
        self.assertEqual([item["icon"] for item in self.team.listing()], ["🤖", "🤖"])
        third = self.team.request("master", "create_agent", {"name": "Third", "icon": "🧭"})["agent_id"]
        self.assertEqual(self.team._agent(third)["icon"], self.team.master_icon)
        self.team.queue_user(first, "Preserve unread input")
        messages = deepcopy(self.team.data["messages"])
        for target in (first, second, "master"):
            self.team.request("master", "set_agent_icon", {"agent_id": target, "icon": "🔬"})
        self.assertEqual(self.team.master_icon, "🔬")
        self.assertEqual(MemberStore(self.store, first).metadata["icon"], "🔬")
        self.assertEqual(self.team.data["messages"], messages)
        self.team.finish(second, "stopped", "pause")
        for cleaner in self.team.cleaners:
            cleaner.join(1)
        fourth = self.team.request("master", "create_agent", {"name": "Fourth", "icon": "🔬"})["agent_id"]
        self.team.request("master", "set_agent_icon", {"agent_id": second, "icon": "🔬️"})
        self.assertEqual(MemberStore(self.store, fourth).metadata["icon"], "🔬")
        self.assertEqual(MemberStore(self.store, second).metadata["icon"], "🔬️")

    def test_human_inline_images_are_snapshotted_once_without_repeated_attachments(self):
        from multiagent.prompts import SYSTEM
        figure=self.store.workspace/'inline.png';figure.write_bytes(png())
        for fields in ({"message":"正文 ![图](/workspace/inline.png)"},
                       {"message":"正文 ![图](/workspace/inline.png)","images":["/workspace/inline.png"]}):
            result=self.team.request("master","send_user_message",fields)
            self.assertEqual(len(result["images"]),1)
            self.assertEqual(self.store.image_asset(result["images"][0]["sha256"])[0],png())
            self.assertEqual(result["images"][0]["source"],"/workspace/inline.png")
        self.assertIn("Do NOT also put an image already embedded",SYSTEM)

    def test_context_requests_delegate_to_session_without_waiting_or_management_restrictions(self):
        identifier = self.create("independent")
        fields = {"model":"grok-4.7","estimated_input_tokens":410000,"trigger_tokens":400000,
                  "context_window_tokens":500000,"output_reserve_tokens":65536,"estimate_method":"local_estimate",
                  "profile_source":"model_profile","last_reported_input_tokens":None,"messages":100,"images":0,"step":1,
                  "snapshot_count":100,"snapshot_sha256":"a"*64}
        from session.compaction import SessionCompaction
        from types import SimpleNamespace
        maintenance = object.__new__(SessionCompaction)
        maintenance.session = SimpleNamespace(team=self.team, master=self.master)
        maintenance.request = Mock(return_value={"status":"queued"})
        for source in ("master",identifier):
            self.assertEqual(maintenance.request_from_agent(source,fields),{"status":"queued"})
            self.assertEqual(maintenance.request.call_args.args[0]["agent_id"],source)
        self.assertNotIn("compaction_requests",self.team.summary())
        self.assertIsNotNone(self.create("while-compressing"))
        self.master.status="stopped"
        with self.assertRaises(ValueError):maintenance.request_from_agent("master",fields)

    def test_cache_totals_pair_usage_and_survive_current_session_statistics_rebuild(self):
        peer = self.create("cache-peer")
        def emit(source, event):
            event = {"run_id": "cache-"+source, "timestamp": time.time(), **event}
            runtime = self.master if source == "master" else self.team.agents[source]
            runtime.publish(event)
            self.team.observe(source, event)
        def completed(source, step, prompt, cached=None, *, nested=False):
            emit(source, {"type": "model_start", "step": step})
            emit(source, {"type": "model_transport", "step": step, "phase": "reading_body", "cached_tokens": 999})
            if not nested:
                emit(source, {"type": "model_transport", "step": step, "phase": "complete", "cached_tokens": cached})
            usage = {"prompt_tokens": prompt, "completion_tokens": 5}
            if nested:
                usage["prompt_tokens_details"] = {"cached_tokens": cached}
            emit(source, {"type": "model_end", "step": step, "elapsed": .1, "usage": usage})
        completed("master", 1, 100, 80)
        completed("master", 2, 50)  # Missing is unknown, not zero cache hits.
        completed("master", 3, 10, 0, nested=True)
        emit("master", {"type": "context_compaction_start", "step": 4, "id": "memory"})
        emit("master", {"type": "context_compaction_transport", "step": 4, "id": "memory", "phase": "complete", "cached_tokens": 25})
        emit("master", {"type": "context_compaction_end", "step": 4, "id": "memory", "elapsed": .1,
                        "usage": {"prompt_tokens": 40, "completion_tokens": 5}})
        completed(peer, 1, 20, 99)  # Invalid report is unknown.
        completed(peer, 2, 30, 10, nested=True)
        expected = {"prompt_tokens": 250, "cached_tokens": 115, "uncached_tokens": 65,
                    "cache_known_input_tokens": 180, "cache_unknown_input_tokens": 70}
        for key, value in expected.items():
            self.assertEqual(self.team.summary()[key], value, key)
        # Re-derive current-format statistics once, using original events, not
        # inferred cache ratios or the 800-event browser preview window.
        self.team.pause()
        self.master.status = "stopped"
        self.team.data["statistics_revision"] = 1
        self.team.data["master"]["runs"] = {}
        self.team._save()
        restored = MultiAgentSystem(self.store, dict(CONFIG), self.factory, self.events.append, self.master)
        self.addCleanup(restored.pause)
        for key, value in expected.items():
            self.assertEqual(restored.summary()[key], value, key)

    def test_roles_group_recipients_and_metadata_privacy(self):
        first, second = self.create("first"), self.create("second")
        self.assertEqual({item["function"]["name"] for item in agent_tools(first)},
            {"send_agent_message", "broadcast_agent_message", "send_user_message", "list_agents", "sleep"})
        tools = HostTools(Mock(), lambda: [], schemas=agent_tools(first))
        for name, fields in (("create_agent", {"name": "denied"}), ("stop_agent", {"agent_id": second, "reason": "denied"}), ("resume_agent", {"agent_id": second})):
            self.assertIn("error", tools.execute(name, json.dumps(fields)))
            with self.assertRaises(ValueError):
                self.team.request(first, name, fields)
        tools.rpc.assert_not_called()
        self.team._agent(first).update(context="PRIVATE", messages=["PRIVATE"], reasoning_content="PRIVATE", task="PRIVATE", error="PRIVATE exception echo")
        listing = self.team.request(second, "list_agents", {})["agents"]
        self.assertEqual([item["id"] for item in listing], ["master", first, second])
        self.assertNotIn("PRIVATE", json.dumps(listing))
        self.assertTrue(listing[1]["error_available"])
        result = self.team.request(first, "send_agent_message", {"agent_ids": ["master", second], "message": "group finding"})
        self.assertEqual([item["agent_id"] for item in result["recipients"]], ["master", second])
        before = deepcopy(self.team.data["messages"])
        for fields in ({"agent_id": second, "agent_ids": [second]}, {"agent_ids": []}, {"agent_ids": [second, second]}, {"agent_ids": [second, "user"]}, {}):
            with self.assertRaises((ValueError, KeyError)):
                self.team.request(first, "send_agent_message", {"message": "invalid", **fields})
        self.assertEqual(self.team.data["messages"], before)
        self.assertFalse(hasattr(self.team, "queue_user_broadcast"))

    def test_stopped_agents_queue_human_text_images_but_reject_peer_mail(self):
        first, peer = self.create("paused"), self.create("peer")
        self.team.request("master", "stop_agent", {"agent_id": first, "reason": "pause"})
        for cleaner in self.team.cleaners:
            cleaner.join(1)
        self.assertEqual(self.team._agent(first)["status"], "stopped")
        calls = self.factory.call_count
        store = MemberStore(self.store, first)
        image = store.add_upload(png(), "queued.png")
        human = self.team.queue_user(first, "Human follow-up while stopped", [image["id"]])
        self.master.status = "stopped"
        master_human = self.team.queue_user("master", "Master follow-up while stopped")
        self.assertEqual(self.factory.call_count, calls, "Human input never resumes workers")
        self.assertNotIn(first, self.team.agents)
        self.assertEqual(self.team._agent(first)["status"], "stopped")
        persisted = json.loads(self.team.path.read_text())["messages"]
        self.assertIn(human["id"], {message["id"] for message in persisted})
        self.assertIn(master_human["id"], {message["id"] for message in persisted})
        self.assertEqual(human["attachments"][0]["sha256"], image["sha256"])
        self.assertIsNone(store.load_state(), "Queuing is not a model/context commit")
        before = deepcopy(self.team.data["messages"])
        for target in (first, "master"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.team.request(peer, "send_agent_message", {"agent_id": target, "message": "must not be queued"})
        self.assertEqual(self.team.data["messages"], before)
        broadcast = self.team.request(peer, "broadcast_agent_message", {"message": "only live recipients"})
        self.assertEqual(broadcast["recipients"], [])
        self.assertEqual(self.team.data["messages"], before)
        self.master.status = "running"
        self.team.request("master", "resume_agent", {"agent_id": first})
        self.assertEqual(self.factory.call_count, calls + 1)
        pending = self.team.pending(first, [])
        unread = next(message for message in pending if message["_agent_message_id"] == human["id"])
        self.assertEqual(unread["content"], "Human follow-up while stopped")
        self.assertEqual(unread["_user_input"]["attachments"][0]["sha256"], image["sha256"])
        self.assertFalse(any(message["_agent_message_id"] == human["id"] for message in self.team.pending(first, [human["id"]])))

    def test_sleep_existing_mail_own_recipient_only_timeout_and_cancellation(self):
        first, second = self.create("first"), self.create("second")
        self.team.queue_user(first, "already unread")
        result = self.team.request(first, "sleep", {"seconds": 3600, "seen": []})
        self.assertEqual((result["reason"], result["messages_queued"]), ("message", 1))
        self.assertLess(result["elapsed"], .1)
        self.assertNotIn("messages", result)
        seen = [item["_agent_message_id"] for item in self.team.pending(first, [])]
        waiting, cancelled = threading.Event(), threading.Event()
        condition = self.team.mailbox(first).condition
        wait = condition.wait
        def tracked(timeout):
            waiting.set()
            return wait(timeout)
        condition.wait = tracked
        results = []
        thread = threading.Thread(target=lambda: results.append(self.team.request(first, "sleep", {"seconds": 1, "seen": seen})))
        thread.start()
        self.assertTrue(waiting.wait(1))
        self.team.request("master", "send_agent_message", {"agent_id": second, "message": "other inbox"})
        self.team.changed()
        time.sleep(.03)
        self.assertTrue(thread.is_alive())
        self.team.request(second, "send_agent_message", {"agent_id": first, "message": "your finding"})
        thread.join(1)
        self.assertEqual(results[0]["reason"], "message")
        seen = [item["_agent_message_id"] for item in self.team.pending(first, [])]
        errors = []
        def sleeping():
            try:
                self.team.request(first, "sleep", {"seconds": 3600, "seen": seen}, cancelled=cancelled.is_set)
            except ValueError as error:
                errors.append(str(error))
        waiting.clear()
        thread = threading.Thread(target=sleeping)
        thread.start()
        self.assertTrue(waiting.wait(1))
        cancelled.set()
        thread.join(.8)
        self.assertFalse(thread.is_alive())
        self.assertIn("cancelled", errors[0])
        self.assertEqual(self.team.sleep(first, 0, seen)["reason"], "timeout")
        for value in (-1, 3601, True, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                self.team.sleep(first, value, seen)

    def test_invalid_sleep_arguments_do_not_publish_nonfinite_agent_metadata(self):
        identifier = self.create("sleep validation")
        for seconds in (float("nan"), float("inf"), -1, 3601, True, "10", None):
            with self.subTest(seconds=seconds):
                self.team.observe(identifier, {"type": "tool_start", "name": "sleep", "arguments": {"seconds": seconds}})
                self.assertEqual(self.team._agent(identifier)["status"], "running")
                self.assertNotIn("sleep", self.team._agent(identifier))
                # Browsers require strict JSON, unlike Python's permissive decoder.
                json.loads(self.team.path.read_text(), parse_constant=lambda value: self.fail(f"Invalid JSON number: {value}"))
        self.team.observe(identifier, {"type": "tool_start", "name": "sleep", "arguments": {"seconds": 0}})
        self.assertEqual(self.team._agent(identifier)["sleep"]["seconds"], 0)

    def test_peer_image_snapshots_are_visible_files_but_not_automatic_model_images(self):
        first, second = self.create("first"), self.create("second")
        (self.root / "plot.png").write_bytes(png())
        result = self.team.request("master", "send_agent_message", {"agent_ids": [first, second], "message": "consider this image", "images": ["/workspace/plot.png"]})
        (self.root / "plot.png").unlink()
        for recipient in result["recipients"]:
            store = MemberStore(self.store, recipient["agent_id"])
            image = recipient["images"][0]
            self.assertEqual(store.image_asset(image["sha256"])[0], png())
            pending = self.team.pending(recipient["agent_id"], [])
            self.assertEqual(image_usage(pending)["images"], 0)
            self.assertNotIn("image_url", json.dumps(to_api_messages(pending)))
            self.assertIn(image["path"], to_api_messages(pending)[0]["content"])
            self.assertEqual(pending[0]["_agent_message"]["images"][0]["path"], image["path"])
            viewed, _ = make_tool_message(pending, "explicit-view", read_image(image["path"], root=self.root))
            self.assertEqual(image_usage([viewed])["images"], 1)
        self.assertEqual(self.store.image_asset(result["images"][0]["sha256"])[0], png())
        before = deepcopy(self.team.data["messages"])
        for path in ("/etc/passwd", "../outside.png", f"{self.store.folder}/images/secret.png"):
            with self.assertRaises((ValueError, OSError)):
                self.team.request("master", "send_agent_message", {"agent_id": first, "message": "blocked", "images": [path]})
        self.assertEqual(self.team.data["messages"], before)

    def test_human_outbox_is_explicit_persistent_and_has_sender_image_assets(self):
        first = self.create("first")
        self.team.observe(first, {"type": "model_end", "run_id": "private", "step": 1, "elapsed": .1, "content": "private model prose"})
        self.assertEqual([m for m in self.team.data["messages"] if m["kind"] == "human_output"], [])
        (self.root / "human.png").write_bytes(png())
        result = self.team.request(first, "send_user_message", {"message": "## Hello human", "images": ["human.png"]})
        (self.root / "human.png").unlink()
        persisted = [m for m in json.loads(self.team.path.read_text())["messages"] if m["kind"] == "human_output"]
        self.assertEqual((persisted[0]["id"], persisted[0]["source"], persisted[0]["target"]), (result["message_id"], first, "user"))
        self.assertTrue(persisted[0]["published"])
        event = self.runtimes[-1].events[-1]
        self.assertEqual(event["type"], "human_message")
        self.assertEqual(event["content"], "## Hello human")
        self.assertNotIn("data_url", json.dumps(event))
        self.assertEqual(MemberStore(self.store, first).image_asset(event["images"][0]["sha256"])[0], png())
        self.assertEqual(self.team.pending("master", []), [])
        self.assertEqual(MemberStore(self.store, first).load_events()[-1]["message_id"], result["message_id"])

    def test_startup_resume_identity_and_deferred_idle_failures_use_queue_once(self):
        first = self.create("worker")
        self.team.request(first, "cycle", {"phase": "startup"})
        startup = self.team.pending(first, [])[0]
        self.assertIn(first, startup["content"])
        self.assertIn("role=SUB", startup["content"])
        before = deepcopy(self.team.data)
        with self.assertRaises(ValueError):
            self.team.request(first, "cycle", {"phase": "turn_end"})
        self.assertEqual(self.team.data, before)
        self.master.status = "stopped"
        settings = self.team.set_idle_sleep(25)
        self.assertEqual(self.store.settings()["idle_sleep_seconds"], 25)
        self.assertIsNone(settings["queued"])
        self.assertIsNotNone(settings["deferred"])
        self.team.observe(first, {"type": "loop_stopped", "status": "failed", "error": "fixture failure"})
        for thread in self.team.cleaners:
            thread.join(1)
        self.assertEqual(self.team.pending("master", []), [])
        deferred = deepcopy([m for m in self.team.data["messages"] if m.get("deferred")])
        self.master.status = "starting"
        self.team.request("master", "cycle", {"phase": "resume"})
        pending = self.team.request("master", "drain", {"seen": []})
        self.assertEqual(pending["idle_sleep_seconds"], 25)
        self.assertEqual([item["_agent_message_id"] for item in pending["messages"][:2]], [item["id"] for item in deferred])
        self.assertIn("role=MASTER", pending["messages"][-1]["content"])
        self.assertEqual([m for m in self.team.data["messages"] if m.get("deferred")], [])
        for value in (0, -.1, 3601, True, float("nan")):
            with self.assertRaises(ValueError):
                self.team.set_idle_sleep(value)

    def test_v5_checkpoint_uses_global_counts_and_preserves_stopped_location(self):
        history = AgentHistory(self.store, system="own", tools=[], model="fixture")
        self.addCleanup(history.close)
        history.record("model_request", step=7, iteration=7)
        history.record("tool_start", step=7, number=9, name="sleep", call_id="pause")
        state = self.store.load_state()
        self.assertEqual((state["iteration"], state["tool_count"], state["run_state"]), (7, 9, "sleeping"))
        history.record("tool_end", step=7, number=9, name="sleep", call_id="pause")
        history.record("agent_stopped", reason="stopped")
        state = self.store.load_state()
        self.assertEqual(state["run_state"], "stopped")
        self.assertEqual(state["location"]["call_id"], "pause")
        # Restore can append interrupted tool placeholders before this event;
        # the runtime note still carries the actual pre-stop execution position.
        history.record("context_resume", messages=state["messages"], iteration=7, tool_count=9,
                       previous_location={"previous_location": state["location"], "note": "fresh runtime"})
        self.assertEqual(self.store.load_state()["location"], state["location"])
        self.assertTrue({"turn_number", "active_turn"}.isdisjoint(state))
        self.assertEqual(VERSION, 5)
        old = SessionStore.create(self.root, "old v4", CONFIG)
        atomic_json(old.folder / "session.json", {**old.metadata, "version": 4})
        original = (old.folder / "session.json").read_bytes()
        with self.assertRaises(UnsupportedSession):
            SessionStore(old.folder)
        self.assertEqual((old.folder / "session.json").read_bytes(), original)

    def test_global_stop_invalidates_inflight_factory_and_preserves_old_mail(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        launched = []
        def factory(store, config, *_):
            runtime = Runtime(store, config)
            launched.append(runtime)
            entered.set()
            release.wait(2)
            return runtime
        self.team.factory = factory
        results = []
        thread = threading.Thread(target=lambda: results.append(self.team.request("master", "create_agent", {"name": "racing"})))
        thread.start()
        self.assertTrue(entered.wait(1))
        identifier = self.team.listing()[0]["id"]
        message = self.team.queue_user(identifier, "accepted before Stop")
        self.master.status = "stopping"
        self.team.stop_children("global Stop")
        self.assertEqual(self.team.summary()["active"], 1)
        queued = self.team.queue_user(identifier, "kept unread after Stop")
        self.master.status = "running"
        release.set()
        thread.join(1)
        for cleanup in self.team.cleaners:
            cleanup.join(1)
        self.assertTrue(launched[0].closed)
        self.assertEqual(self.team._agent(identifier)["status"], "stopped")
        self.assertEqual(self.team.pending(identifier, [])[0]["_agent_message_id"], message["id"])
        self.assertIn(queued["id"], {item["_agent_message_id"] for item in self.team.pending(identifier, [])})
        self.assertEqual(set(self.team.agents), {"master"})
