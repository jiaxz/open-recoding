"""Aggregate correspondence is read-only; human fan-out keeps real provenance."""

from copy import deepcopy
from pathlib import Path
import threading
import unittest

from workspace_support import temporary_workspace
from unittest.mock import Mock, patch

from team_fixtures import fixture_team as MultiAgentSystem
from session import Session
from session.store import SessionStore
from test_images import png
from test_teams import CONFIG, Runtime


class SessionMessagesTests(unittest.TestCase):
    def setUp(self):
        temporary = temporary_workspace()
        self.addCleanup(temporary.cleanup)
        self.store = SessionStore.create(Path(temporary.name), "messages", CONFIG)
        self.session = object.__new__(Session)
        self.session.store, self.session.config = self.store, dict(CONFIG)
        self.session.master = Runtime(self.store, dict(CONFIG))
        self.session.master.condition = threading.Condition(threading.RLock())
        self.events = []
        def factory(store, config, *_):
            runtime = Runtime(store, config)
            runtime.condition = threading.Condition(threading.RLock())
            return runtime
        self.factory = Mock(side_effect=factory)
        self.team = self.session.team = MultiAgentSystem(self.store, dict(CONFIG), self.factory,
                                                         self.events.append, self.session.master)
        self.addCleanup(self.team.pause)

    def create(self, name):
        identifier = self.team.request("master", "create_agent", {"name": name})["agent_id"]
        self.team.observe(identifier, {"type": "loop_started"})
        return identifier

    def test_feed_orders_pages_without_context_system_notices_or_consumption(self):
        child = self.create("Research")
        self.store.save_state({"messages": [{"role": "system", "content": "PRIVATE SYSTEM"}], "iteration": 0})
        user = self.team.queue_user("master", "User instruction")
        sent = self.team.request("master", "send_agent_message", {"agent_id": child, "message": "Peer evidence"})
        output = self.team.request(child, "send_user_message", {"message": "## Human report"})
        self.team.set_limit(2)
        before = deepcopy(self.team.data)
        with patch.object(SessionStore, "load_state", side_effect=AssertionError("Feed cannot load context")):
            page = self.session.messages(limit=2)
            earlier = self.session.messages(before=page["before"], limit=2)
            after = self.session.messages(after=earlier["after"], limit=2)
            start = self.session.messages(after="start", limit=1)
        self.assertTrue(page["has_more"])
        self.assertEqual([m["id"] for m in page["messages"]], [sent["recipients"][0]["message_id"], output["message_id"]])
        self.assertEqual([m["id"] for m in earlier["messages"]], [user["id"]])
        self.assertEqual(after["messages"], page["messages"])
        self.assertFalse(after["has_more"])
        self.assertEqual(start["messages"][0]["id"], user["id"])
        self.assertEqual(before, self.team.data)
        self.assertEqual({m["kind"] for m in [*page["messages"], *earlier["messages"]]}, {"human_input", "peer", "human_output"})
        self.assertNotIn("PRIVATE SYSTEM", str(page))
        self.assertNotIn("System", str(page))
        self.assertEqual(self.team.pending("master", [])[0]["_agent_message_id"], user["id"])

    def test_human_all_copies_image_assets_only_to_living_members(self):
        active, stopped = self.create("Active"), self.create("Stopped")
        self.team.finish(stopped, "stopped", "pause")
        for cleaner in self.team.cleaners:
            cleaner.join(1)
        picture = self.store.add_upload(png(), "clipboard.png")
        factories = self.factory.call_count
        result = self.session.submit_message("**Shared instruction**", [picture["id"]], recipient="all")
        self.assertEqual([item["agent_id"] for item in result["recipients"]], ["master", active])
        self.assertIsNotNone(result["broadcast_id"])
        self.assertEqual(self.factory.call_count, factories, "Human mail cannot start stopped workers")
        self.assertEqual(self.team._agent(stopped)["status"], "stopped")
        feed = self.session.messages()["messages"]
        self.assertEqual(len(feed), 2)
        self.assertEqual({m["broadcast_id"] for m in feed}, {result["broadcast_id"]})
        self.assertEqual({m["broadcast_scope"] for m in feed}, {"all"})
        self.assertFalse(self.team.pending(stopped, []))
        for item in feed:
            self.assertEqual(item["source"], "user")
            self.assertEqual(item["kind"], "human_input")
            self.assertEqual(item["image_owner"], item["target"])
            store = self.store if item["target"] == "master" else self.session.agent_store(item["target"])[0]
            self.assertEqual(store.image_asset(item["images"][0]["sha256"])[0], png())
            pending = self.team.pending(item["target"], [])
            user = next(m for m in pending if m.get("_user_input"))
            self.assertEqual(user["_user_input"]["attachments"][0]["sha256"], picture["sha256"])
        self.assertEqual(self.team.sleep("master", 30, [])["reason"], "message")

    def test_all_except_master_and_stale_selections_exclude_nonliving_agents(self):
        active, stopped, stopping, failed = [self.create(name) for name in ("Active", "Stopped", "Stopping", "Failed")]
        self.team._agent(stopped)["status"] = "stopped"
        self.team._agent(stopping)["status"] = "stopping"
        self.team._agent(failed)["status"] = "failed"
        for target in (stopped, stopping, failed):
            with self.assertRaises(ValueError):
                self.session.submit_message("Reject stale selection", recipient=target)
        result = self.session.submit_message("Subs only", recipient="all_except_master")
        self.assertEqual([item["agent_id"] for item in result["recipients"]], [active])
        self.assertEqual(self.session.messages()["messages"][0]["broadcast_scope"], "all_except_master")
        self.assertFalse(self.team.pending("master", []))
        self.session.master.status = "stopped"
        result = self.session.submit_message("Only living", recipient="all")
        self.assertEqual([item["agent_id"] for item in result["recipients"]], [active])
        self.team._agent(active)["status"] = "stopped"
        before = deepcopy(self.team.data["messages"])
        for recipient in ("all", "all_except_master", "master"):
            with self.assertRaises(ValueError):
                self.session.submit_message("No recipients", recipient=recipient)
        self.assertEqual(self.team.data["messages"], before)
        # Dedicated Agent pages retain the separate stopped-user-mail behavior.
        self.team.queue_user(stopped, "Leave a message on the stopped Agent page")
        self.assertEqual(len(self.team.pending(stopped, [])), 1)

    def test_broadcast_rechecks_liveness_after_preparing_images(self):
        active = self.create("Stops during upload")
        picture = self.store.add_upload(png(), "clipboard.png")
        from session.session import copy_images
        def copy_and_stop(store, pictures):
            copies = copy_images(store, pictures)
            self.team._agent(active)["status"] = "stopped"
            return copies
        with patch("session.session.copy_images", side_effect=copy_and_stop):
            result = self.session.submit_message("Race with Stop", [picture["id"]], recipient="all")
        self.assertEqual([item["agent_id"] for item in result["recipients"]], ["master"])
        self.assertFalse(self.team.pending(active, []))

    def test_default_and_directed_human_inputs_do_not_broadcast_or_escalate(self):
        child = self.create("Only recipient")
        self.session.submit_message("Default Master")
        self.session.submit_message("Only child", recipient=child)
        self.assertEqual([(m["target"], m["content"]) for m in self.session.messages()["messages"]],
                         [("master", "Default Master"), (child, "Only child")])
        with self.assertRaises(ValueError):
            self.team.request(child, "queue_user", {"content": "fake human", "recipient": "all"})
        for fields in ({"limit": 0}, {"limit": 201}, {"before": "missing"}, {"before": "a", "after": "b"}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.session.messages(**fields)


if __name__ == "__main__":
    unittest.main()
