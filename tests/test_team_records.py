"""One state authority, common lifecycle and durable publication regressions."""
from pathlib import Path
import json
import threading
import unittest
from unittest.mock import patch

from multiagent import MultiAgentSystem
from multiagent.storage import StoreLease, atomic_json, load_team
from session import SavedSession
from session.store import SessionStore
from test_team_runtime import CONFIG, wait_for
from workspace_support import temporary_workspace


class TeamRecordTests(unittest.TestCase):
    def team(self, **options):
        workspace = temporary_workspace(prefix="recoding-records-")
        self.addCleanup(workspace.cleanup)
        store = SessionStore.create(Path(workspace.name),"records",CONFIG)
        team = MultiAgentSystem(store,dict(CONFIG),**options)
        self.addCleanup(team.close)
        return team

    def test_master_and_sub_properties_are_views_of_the_same_persistent_record(self):
        team = self.team()
        team.start()
        wait_for(lambda:team.master.status == "sleeping",lambda:team.master.snapshot())
        child = team.request("master","create_agent",{"name":"Peer"})["agent_id"]
        runtime = team.agents[child]
        wait_for(lambda:runtime.status == "sleeping",lambda:runtime.snapshot())
        for identifier in ("master",child):
            actor = team.agents[identifier]
            self.assertIs(actor.record,team.record(identifier))
            self.assertFalse({"status","run_id","error","sleep"} & vars(actor).keys())
            self.assertEqual(actor.status,actor.record["status"])
        team.stop()
        wait_for(lambda:runtime.status == team.master.status == "stopped",lambda:team.public_agents())
        runtime.record["status"] = "failed"
        self.assertEqual(runtime.status,"failed")
        self.assertEqual(next(item for item in team.public_agents() if item["id"] == child)["status"],"failed")
        runtime.status = "stopped"
        self.assertEqual(team.record(child)["status"],"stopped")
        with team.condition:team._save()
        saved = json.loads(team.path.read_text())
        self.assertEqual(saved["master"]["status"],"stopped")
        self.assertNotIn("master_runs",saved)
        self.assertNotIn("master_icon",saved)

    def test_master_resume_waits_for_the_same_cleanup_barrier_as_subs(self):
        entered,released = threading.Event(),threading.Event()
        self.addCleanup(released.set)
        def release(identifier):
            if identifier == "master":
                entered.set()
                released.wait(5)
        team = self.team(release_owner=release)
        team.start()
        wait_for(lambda:team.master.status == "sleeping",lambda:team.master.snapshot())
        previous = team.record("master")["attempt"]
        process = team.master.process
        team.stop()
        self.assertTrue(entered.wait(2))
        self.assertEqual(team.master.status,"stopping")
        self.assertIsNotNone(team.master.lease.fd)
        with self.assertRaises(ValueError):team.start()
        released.set()
        wait_for(lambda:team.master.status == "stopped",lambda:team.master.snapshot())
        self.assertIsNotNone(process.poll())
        team.start()
        wait_for(lambda:team.master.status == "sleeping",lambda:team.master.snapshot())
        self.assertEqual(team.record("master")["attempt"],previous+1)
        self.assertNotEqual(team.master.process.pid,process.pid)
        with self.assertRaises(KeyError):
            team.request("master","stop_agent",{"agent_id":"master","reason":"not a sub"})
        self.assertEqual(team.master.status,"sleeping")

    def test_publication_is_once_and_recovers_the_append_before_mark_window(self):
        team = self.team()
        with team.condition:
            message = team._enqueue("user","master","kept while stopped","user_input",attachments=[])
            team._save()
        errors = []
        def publish():
            try:team._publish_message(message)
            except Exception as error:errors.append(error)
        threads = [threading.Thread(target=publish) for _ in range(5)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(3)
        self.assertFalse(errors)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sum(item.get("message_id") == message["id"] for item in team.store.iter_events()),1)
        self.assertTrue(message["published"])
        message["published"] = False
        team._save()
        team._publish_message(message,recover=True)
        self.assertEqual(sum(item.get("message_id") == message["id"] for item in team.store.iter_events()),1)
        self.assertIsNone(team.master.process)
        self.assertEqual(team.pending("master",[])[0]["content"],"kept while stopped")

    def test_spawn_and_partial_start_failures_use_the_common_cleanup(self):
        for failure in ("spawn", "initial input"):
            with self.subTest(failure=failure):
                team = self.team()
                target = "multiagent.agent.subprocess.Popen" if failure == "spawn" else "multiagent.agent.Agent._write"
                with patch(target,side_effect=OSError("synthetic " + failure)):
                    with self.assertRaises(OSError):team.start()
                    wait_for(lambda:team.master.status == "failed",lambda:team.master.snapshot())
                self.assertIsNotNone(team.master.lease.fd)
                if team.master.process:self.assertIsNotNone(team.master.process.poll())
                team.start()
                wait_for(lambda:team.master.status == "sleeping",lambda:team.master.snapshot())
                with patch("multiagent.agent.subprocess.Popen",side_effect=OSError("child spawn failed")):
                    child = team.request("master","create_agent",{"name":"Broken"})["agent_id"]
                    wait_for(lambda:team.record(child)["status"] == "failed",lambda:team.listing())
                self.assertNotIn(child,team.agents)
                self.assertTrue(StoreLease.available(team.folder/child))
                self.assertTrue(any(item.get("configuration",{}).get("agent_id") == child for item in team.data["messages"]))
                team.close()

    def test_current_version_old_index_is_normalized_without_read_only_writes(self):
        team = self.team()
        folder = team.store.folder
        team.close()
        old = json.loads(team.path.read_text())
        old["master_icon"] = old["master"]["icon"]
        old["master_runs"] = old.pop("master")["runs"]
        old["human_outbox"] = [{"id":"report","source":"master","target":"user","content":"report", "timestamp":1,"published":True,"images":[]}]
        old["deferred_system"] = [{"id":"notice","source":"system","target":"master","kind":"system_message","content":"later","timestamp":2,"configuration":{}}]
        atomic_json(team.path,old)
        before = team.path.read_bytes()
        saved = SavedSession(folder)
        self.assertEqual(saved.messages()["messages"][0]["content"],"report")
        self.assertEqual(team.path.read_bytes(),before)
        data = load_team(team.store)
        self.assertFalse({"human_outbox","deferred_system","master_runs","master_icon"} & data.keys())
        self.assertEqual([item["kind"] for item in data["messages"]],["human_output","system_message"])
        self.assertTrue(data["messages"][1]["deferred"])

    def test_stop_during_construction_publishes_accepted_mail_without_starting_child(self):
        team = self.team()
        team.start()
        wait_for(lambda:team.master.status == "sleeping",lambda:team.master.snapshot())
        entered,released = threading.Event(),threading.Event()
        self.addCleanup(released.set)
        factory,created = team.factory,[]
        def delayed(store, config, owner, identifier):
            entered.set()
            released.wait(5)
            runtime = factory(store,config,owner,identifier)
            created.append(runtime)
            return runtime
        team.factory = delayed
        thread = threading.Thread(target=lambda:team.request("master","create_agent",{"name":"Slow"}))
        thread.start()
        self.assertTrue(entered.wait(2))
        child = team.listing()[0]["id"]
        message = team.queue_user(child,"Accepted before Stop")
        self.assertFalse(message["published"])
        team.stop()
        released.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        wait_for(lambda:team.record(child)["status"] == "stopped",lambda:team.listing())
        wait_for(lambda:any(item.get("event",{}).get("message_id") == message["id"] for item in team.event_stream.events),lambda:list(team.event_stream.events)[-3:])
        self.assertIsNone(created[0].process)
        self.assertEqual(team.pending(child,[])[0]["content"],"Accepted before Stop")

    def test_stop_during_worker_preparation_does_not_spawn_a_late_worker(self):
        team = self.team()
        entered,released = threading.Event(),threading.Event()
        self.addCleanup(released.set)
        original = team.master._retire_worker
        first = True
        def prepare():
            nonlocal first
            if first:
                first = False
                entered.set()
                released.wait(5)
            return original()
        errors = []
        def start():
            try:team.start()
            except ValueError as error:errors.append(str(error))
        with patch.object(team.master,"_retire_worker",side_effect=prepare), patch.object(team.master,"_launch") as launch:
            thread = threading.Thread(target=start)
            thread.start()
            self.assertTrue(entered.wait(2))
            team.stop()
            self.assertEqual(team.master.status,"stopping")
            released.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            wait_for(lambda:team.master.status == "stopped",lambda:team.master.snapshot())
            launch.assert_not_called()
        self.assertEqual(len(errors),1)
        self.assertIsNone(team.master.process)
        with patch.object(team.master,"_write") as write:
            team.master._begin_run()  # A delayed Ready must close, never send Run.
            write.assert_called_once_with({"action":"close"})
        self.assertEqual(team.master.status,"stopped")
