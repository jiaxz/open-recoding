"""Opt-in Master observation, bounded tokens and real local-worker RPC."""

from copy import deepcopy
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from multiagent.tools import HostTools
from team_fixtures import fixture_team as MultiAgentSystem
from multiagent.tools import agent_tools
import test_teams as team_fixture
from test_images import png

CONFIG = team_fixture.CONFIG


class GlobalMessageTests(unittest.TestCase):
    setUp = team_fixture.TeamTests.setUp
    create = team_fixture.TeamTests.create

    def read(self, **fields):
        return self.team.request("master", "read_global_messages", fields)

    def send(self, source, target, content):
        return self.team.request(source, "send_agent_message", {"agent_id": target, "message": content})

    def test_no_cc_no_automatic_delivery_and_subs_cannot_observe(self):
        a, b = self.create("A"), self.create("B")
        for source in ("master", a):
            schemas = {item["function"]["name"]: item["function"] for item in agent_tools(source)}
            for name in ("send_agent_message", "send_user_message"):
                self.assertNotIn("cc", schemas[name]["parameters"]["properties"])
                fields = {"message": "not sent", "cc": ["master"]}
                if name == "send_agent_message":
                    fields["agent_id"] = b
                with self.assertRaises(ValueError):
                    self.team.request(source, name, fields)
        self.send(a, b, "A task for B")
        self.team.request(a, "send_user_message", {"message": "A report for human"})
        self.assertEqual(self.team.pending("master", []), [])
        before = deepcopy(self.team.data)
        page = self.read(after=0)
        self.assertEqual([(m["from"], m["to"]) for m in page["messages"]], [(a, [b]), (a, ["user"])])
        self.assertEqual(self.team.data, before, "Observing must not mark, forward or consume messages")
        tools = HostTools(lambda *_: self.fail("Forbidden tool reached RPC"), lambda: [], agent_tools(a))
        for action in ("read_global_messages", "listen_global_messages"):
            with self.assertRaises(ValueError):
                self.team.request(a, action, {})
            self.assertIn("error", tools.execute(action, "{}"))

    def test_grouping_incremental_cursor_filters_and_resume_ignore_clock_order(self):
        a, b = self.create("A"), self.create("B")
        for text in ("first", "second", "third"):
            with patch("multiagent.system.time.time", return_value=10):
                self.send(a, b, text)
        first = self.read(after=0, limit=2)
        self.assertTrue(first["has_more"])
        self.assertEqual([m["content"] for m in first["messages"]], ["first", "second"])
        last = self.read(after=first["next_cursor"])
        self.assertEqual([m["content"] for m in last["messages"]], ["third"])
        recent = self.read(limit=1)
        self.assertTrue(recent["earlier_available"])
        self.assertEqual(recent["messages"][0]["content"], "third")
        group = self.team.request(a, "send_agent_message", {"agent_ids": [b, "master"], "message": "group"})
        filtered = self.read(after=last["next_cursor"], senders=[a], recipients=["master"])
        self.assertEqual(len(filtered["messages"]), 1)
        self.assertEqual(filtered["messages"][0]["to"], [b, "master"])
        self.assertEqual(len(group["recipients"]), 2)
        cursor = filtered["next_cursor"]
        self.team.pause()
        restored = MultiAgentSystem(self.store, dict(CONFIG), self.factory, self.events.append, self.master)
        self.addCleanup(restored.pause)
        restored.master.status = "running"  # The fixture explicitly resumes its synthetic Master.
        with patch("multiagent.system.time.time", return_value=1):
            restored.queue_user(b, "new input after resume")
        page = restored.request("master", "read_global_messages", {"after": cursor})
        self.assertEqual([m["content"] for m in page["messages"]], ["new input after resume"])
        self.assertGreater(page["next_cursor"], cursor)

    def test_body_budget_sliced_reads_and_images_are_metadata_only(self):
        a = self.create("Artist")
        (self.root / "plot.png").write_bytes(png())
        content = "长内容" * 400
        self.team.request(a, "send_user_message", {"message": content, "images": ["plot.png"]})
        page = self.read(after=0, max_chars=256)
        item = page["messages"][0]
        self.assertTrue(item["truncated"])
        self.assertEqual(page["body_chars"], 256)
        text = item["content"]
        while item.get("truncated"):
            item = self.read(message_id=item["message_id"], offset=item["next_offset"], max_chars=256)["message"]
            text += item["content"]
        self.assertEqual(text, content)
        image = item["images"][0]
        self.assertTrue((self.root / image["path"].removeprefix("/workspace/")).is_file())
        self.assertNotIn("data_url", json.dumps(item))
        self.assertNotIn("base64", json.dumps(item))
        self.assertEqual(self.team.pending("master", []), [])
        for fields in ({"limit": 0}, {"max_chars": 255}, {"after": True}, {"after": 99999},
                       {"senders": ["system"]}, {"recipients": [a, a]}, {"offset": 1},
                       {"message_id": item["message_id"], "after": 0}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.read(**fields)

    def test_listen_filters_timeout_own_inbox_and_cancellation_without_bodies(self):
        a, b = self.create("A"), self.create("B")
        after = self.read()["next_cursor"]
        result, errors, started = [], [], threading.Event()
        def listen(**extra):
            try:
                started.set()
                result.append(self.team.request("master", "listen_global_messages",
                    {"after": after, "seconds": 2, "senders": [a], **extra}))
            except ValueError as error:
                errors.append(str(error))
        thread = threading.Thread(target=listen);thread.start();self.assertTrue(started.wait(1))
        self.team.set_limit(3)  # A real own-inbox notice must wake even though sender filter excludes system.
        thread.join(1);self.assertFalse(thread.is_alive())
        self.assertEqual(result.pop()["reason"], "inbox_message")
        seen = [item["id"] for item in self.team.data["messages"] if item["target"] == "master"]
        thread = threading.Thread(target=lambda: listen(seen=seen));thread.start()
        self.send(b, a, "unmatched")
        time.sleep(.03);self.assertTrue(thread.is_alive())
        self.send(a, b, "matched global secret")
        thread.join(1);self.assertFalse(thread.is_alive())
        signal = result.pop()
        self.assertEqual(signal["reason"], "global_message")
        self.assertEqual(signal["read_after"], after)
        self.assertNotIn("matched global secret", json.dumps(signal))
        page = self.read(after=signal["read_after"], senders=[a])
        self.assertEqual(page["messages"][0]["content"], "matched global secret")
        timeout = self.team.request("master", "listen_global_messages", {"seconds": 0, "seen": seen})
        self.assertEqual(timeout["reason"], "timeout")
        cancel = threading.Event()
        # A blocked RPC must release the routing lock and exit on Stop/disconnect.
        def cancelled_wait():
            try:
                self.team.request("master", "listen_global_messages", {"seconds": 10, "seen": seen}, cancelled=cancel.is_set)
            except ValueError as error:
                errors.append(str(error))
        thread = threading.Thread(target=cancelled_wait);thread.start();time.sleep(.03);cancel.set()
        thread.join(1);self.assertFalse(thread.is_alive());self.assertEqual(len(errors), 1)


class GlobalMessageRuntimeTests(unittest.TestCase):
    def test_local_worker_reads_global_report_only_via_explicit_tool(self):
        from model_support import local_model
        from session import Session
        from workspace_support import temporary_workspace
        def respond(body, number):
            def call(name, fields):
                return {"role": "assistant", "tool_calls": [{"index": 0, "id": str(number), "type": "function",
                    "function": {"name": name, "arguments": json.dumps(fields)}}]}
            master = "Your role is MASTER;" in body["messages"][0]["content"]
            tools = [json.loads(m["content"]) for m in body["messages"] if m["role"] == "tool"]
            if not master:
                return call("sleep", {"seconds": 300}) if tools else call("send_user_message", {"message": "GLOBAL_ONLY_REPORT"})
            if not tools:
                return call("create_agent", {"name": "Reporter"})
            if any(item.get("target") == "user" for item in tools):
                return call("sleep", {"seconds": 300})
            for item in tools:
                if item.get("scope") == "observed_correspondence" and any(m["content"] == "GLOBAL_ONLY_REPORT" for m in item.get("messages", [])):
                    return call("send_user_message", {"message": "MASTER_OBSERVED_REPORT"})
            signal = next((item for item in reversed(tools) if "read_after" in item), None)
            if signal:
                return call("read_global_messages", {"after": signal["read_after"]})
            return call("listen_global_messages", {"after": 0, "seconds": 5})
        with temporary_workspace(prefix="global-message-worker-") as root, local_model(Path(root) / "model", respond) as model:
            with patch.dict(os.environ, {"GEMINI_API_KEY": "local-test-only", "SSL_CERT_FILE": str(model.cert),
                                         "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}):
                session = Session.create(Path(root) / "workspace", "Global observation", config={
                    "base_url": model.url, "network": False, "gpu": False, "api_timeout": 5, "tool_timeout": 5})
                try:
                    deadline = time.monotonic() + 15
                    while not any(m["content"] == "MASTER_OBSERVED_REPORT" for m in session.messages()["messages"]):
                        self.assertLess(time.monotonic(), deadline, (session.master.error, model.errors))
                        time.sleep(.03)
                    master_requests = [r for r in model.requests if "Your role is MASTER;" in r["messages"][0]["content"]]
                    self.assertFalse(any("GLOBAL_ONLY_REPORT" in str(m["content"]) for r in master_requests for m in r["messages"] if m["role"] == "user"))
                    self.assertTrue(any("GLOBAL_ONLY_REPORT" in str(m["content"]) for r in master_requests for m in r["messages"] if m["role"] == "tool"))
                    self.assertFalse(any(m["source"] not in {"system", "master", "user"} and m["target"] == "master" for m in session.team.data["messages"]))
                    self.assertEqual(model.errors, [])
                finally:
                    session.close()


if __name__ == "__main__":
    unittest.main()
