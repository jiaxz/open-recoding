"""Native team ownership, event publication and real worker lifecycle regressions."""

from copy import deepcopy
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

from multiagent import MultiAgentSystem
from multiagent.events import EventStream
from multiagent.storage import StoreLease
from session import Session, SavedSession
from session.store import SessionStore
from workspace_support import temporary_workspace


CONFIG = {"demo": True, "network": False, "gpu": False, "model": "grok-4.7",
          "tool_timeout": 5, "api_timeout": 10, "max_output": 16000, "max_subagents": 4}


def wait_for(test, message, seconds=12):
    deadline = time.monotonic() + seconds
    while not test():
        if time.monotonic() >= deadline:
            raise AssertionError(message())
        time.sleep(.01)


class TeamRuntimeTests(unittest.TestCase):
    def workspace(self):
        workspace = temporary_workspace(prefix="recoding-team-runtime-")
        self.addCleanup(workspace.cleanup)
        return Path(workspace.name)

    def test_team_owns_master_and_publishes_without_using_master_method(self):
        store = SessionStore.create(self.workspace(), "ownership", CONFIG)
        team = MultiAgentSystem(store, dict(CONFIG))
        self.addCleanup(team.close)
        self.assertEqual(set(team.agents), {"master"})
        self.assertIsNone(team.master.process)
        self.assertIs(team.master.event_stream, team.event_stream)
        with patch.object(team.master, "publish", side_effect=AssertionError("Master is not the team event bus")):
            team.publish({"type": "team-only-observation"})
        self.assertEqual(team.event_stream.events[-1]["type"], "team-only-observation")
        self.assertEqual(store.load_events()[-1]["type"], "team-only-observation")

    def test_concurrent_events_are_unique_ordered_and_observers_cannot_mutate_journal(self):
        store = SessionStore.create(self.workspace(), "events", CONFIG)
        def observer(event):
            event["payload"]["worker"] = "corrupted"
        stream = EventStream(store, on_event=observer)
        original = {"type": "parallel-observation", "payload": {"worker": "original"}}
        saved = deepcopy(original)
        threads = [threading.Thread(target=lambda: [stream.publish(original) for _ in range(30)]) for _ in range(6)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        events = store.load_events()
        self.assertEqual([event["sequence"] for event in events], list(range(1, 181)))
        self.assertEqual(len(stream.events), 180)
        self.assertTrue(all(event["payload"]["worker"] == "original" for event in events))
        self.assertEqual(original, saved)
        restored = EventStream(store)
        restored.publish({"type": "after-reopen"})
        self.assertEqual(restored.sequence, 181)

    def test_session_messages_stop_resume_and_browse_use_same_team_and_preserve_history(self):
        workspace = self.workspace()
        session = Session.create(workspace, "lifecycle", config=dict(CONFIG))
        self.addCleanup(session.close)
        wait_for(lambda: session.master.status == "sleeping", lambda: session.master.snapshot())
        child = session.team.request("master", "create_agent", {"name": "Researcher", "icon": "🔬"})["agent_id"]
        runtime = session.team.agents[child]
        wait_for(lambda: runtime.status == "sleeping", lambda: runtime.snapshot())
        self.assertIsNot(runtime.event_stream, session.team.event_stream)
        queued = session.submit("Report what you can observe", agent_id=child)["queued"]
        wait_for(lambda: any(message["source"] == child for message in session.messages()["messages"]),
                 lambda: session.messages())
        self.assertTrue(any(event["type"] == "agent_event" and event["agent_id"] == child for event in session.events))
        self.assertTrue(any(message.get("_agent_message_id") == queued for message in runtime.store.load_state()["messages"]))
        master_pid = session.master.process.pid
        child_process = runtime.process
        session.cancel()
        wait_for(lambda: session.master.status == "stopped", lambda: session.master.status)
        wait_for(lambda: child not in session.team.agents, lambda: session.team.listing())
        self.assertIsNotNone(child_process.poll())
        session.start()
        wait_for(lambda: session.master.status == "sleeping", lambda: session.master.snapshot())
        self.assertNotEqual(session.master.process.pid, master_pid)
        self.assertEqual(set(session.team.agents), {"master"})
        folder = session.store.folder
        sequences = [event["sequence"] for event in session.events]
        self.assertEqual(sequences, sorted(set(sequences)))
        session.close()
        saved = SavedSession(folder)
        self.assertTrue(any(message["source"] == child for message in saved.messages()["messages"]))
        self.assertTrue(any(event["type"] == "agent_event" for event in saved.snapshot()["events"]))

    def test_registered_worker_start_failure_is_reclaimed_and_notified(self):
        team = MultiAgentSystem(SessionStore.create(self.workspace(), "failed-start", CONFIG), dict(CONFIG))
        self.addCleanup(team.close)
        team.start()
        wait_for(lambda: team.master.status == "sleeping", lambda: team.master.snapshot())
        created = []
        original = team.factory
        def factory(store, config, owner, identifier):
            runtime = original(store, config, owner, identifier)
            created.append(runtime)
            runtime.start_worker = lambda: (_ for _ in ()).throw(RuntimeError("synthetic start failure"))
            return runtime
        team.factory = factory
        identifier = team.request("master", "create_agent", {"name": "Broken"})["agent_id"]
        wait_for(lambda: team._agent(identifier)["status"] == "failed", lambda: team.listing())
        self.assertNotIn(identifier, team.agents)
        self.assertTrue(created[0].closed)
        self.assertIsNone(created[0].lease.fd)
        notices = [item for item in team.data["messages"] if item.get("configuration", {}).get("agent_id") == identifier]
        self.assertEqual(len(notices), 1)
        self.assertIn("synthetic start failure", notices[0]["content"])

    def test_builtin_dispatch_stays_in_team_and_rejects_sub_management(self):
        resources = Mock(side_effect=AssertionError("Builtin escaped to Session resources"))
        team = MultiAgentSystem(SessionStore.create(self.workspace(), "routing", CONFIG), dict(CONFIG),
                                resource_request=resources)
        self.addCleanup(team.close)
        team.start()
        wait_for(lambda: team.master.status == "sleeping", lambda: team.master.snapshot())
        child = team.dispatch("master", "create_agent", {"name": "Peer"})["agent_id"]
        wait_for(lambda: child in team.agents and team.agents[child].status == "sleeping", lambda: team.listing())
        self.assertIs(team.agents[child].team, team)
        listing = team.dispatch(child, "list_agents", {})
        self.assertEqual({item["id"] for item in listing["_model_result"]["agents"]}, {"master", child})
        sent = team.dispatch(child, "send_user_message", {"message": "A useful report"})
        self.assertEqual(sent["_model_result"]["message_id"], sent["message_id"])
        self.assertEqual(team.conversation()["messages"][-1]["source"], child)
        for action, fields in [("create_agent", {"name": "Forbidden"}),
                               ("stop_agent", {"agent_id": "master", "reason": "Forbidden"})]:
            with self.assertRaisesRegex(ValueError, "Only Master"):
                team.dispatch(child, action, fields)
        resources.assert_not_called()

    def test_resource_dispatch_preserves_caller_and_cancellation(self):
        resources = Mock(return_value={"stdout": "result", "exit_code": 0})
        team = MultiAgentSystem(SessionStore.create(self.workspace(), "resources", CONFIG), dict(CONFIG),
                                resource_request=resources)
        self.addCleanup(team.close)
        cancelled = lambda: False
        fields = {"command": "printf result"}
        self.assertEqual(team.dispatch("master", "bash", fields, cancelled=cancelled)["stdout"], "result")
        resources.assert_called_once_with("master", "bash", fields, cancelled=cancelled)
        resources.reset_mock()
        with self.assertRaisesRegex(ValueError, "cancelled"):
            team.dispatch("master", "bash", fields, cancelled=lambda: True)
        with self.assertRaisesRegex(ValueError, "fields"):
            team.dispatch("master", "bash", [])
        resources.assert_not_called()

    def test_member_observation_failure_releases_lease_and_reports_failure(self):
        team = MultiAgentSystem(SessionStore.create(self.workspace(), "failed-observation", CONFIG), dict(CONFIG))
        self.addCleanup(team.close)
        team.start()
        wait_for(lambda: team.master.status == "sleeping", lambda: team.master.snapshot())
        with patch("multiagent.system.EventStream", side_effect=OSError("synthetic journal failure")):
            child = team.dispatch("master", "create_agent", {"name": "Broken journal"})["agent_id"]
            wait_for(lambda: team._agent(child)["status"] == "failed", lambda: team.listing())
        self.assertNotIn(child, team.agents)
        self.assertTrue(StoreLease.available(team.folder / child))
        notices = [item for item in team.data["messages"] if item.get("configuration", {}).get("agent_id") == child]
        self.assertEqual(len(notices), 1)
        self.assertIn("synthetic journal failure", notices[0]["content"])


if __name__ == "__main__":
    unittest.main()
