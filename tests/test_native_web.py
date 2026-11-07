"""Native queue-only runtime checks with real workers and an isolated TLS model."""

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import threading
import time
import unittest

from workspace_support import temporary_workspace
from unittest.mock import Mock, patch
from types import SimpleNamespace

from session.store import SessionStore
from session import Session
from recoding_web.host import GuiSessionHost
from recoding_web.server import GuiHttpServer
from test_images import png


class NativeWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_workspace(prefix="recoding-native-web-")
        self.addCleanup(self.temp.cleanup)
        clean = {key: value for key, value in os.environ.items() if not key.startswith("GEMINI_")}
        self.environment = patch.dict(os.environ, clean, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.root = Path(self.temp.name) / "workspace"
        self.app = GuiSessionHost(self.root, demo=True, network=False, gpu=False, tool_timeout=5)
        self.server = GuiHttpServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.app.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, path, body=None, *, data=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=15)
        headers = {"X-recoding-Page":"test-page", "X-Agent-Token": self.server.token, "Content-Type": "image/png" if data else "application/json"}
        payload = data if data is not None else json.dumps(body) if body is not None else None
        connection.request("POST" if payload is not None else "GET", path, payload, headers)
        response = connection.getresponse()
        status, raw = response.status, response.read()
        connection.close()
        return status, json.loads(raw)

    def wait(self, identifier, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = self.app.get(identifier).snapshot()
            if predicate(snapshot):
                return snapshot
            if snapshot["master_status"] == "failed":
                self.fail("Native worker failed: " + str(snapshot["error"]))
            time.sleep(.01)
        self.fail("Native runtime timeout: " + repr({key: snapshot.get(key) for key in ("status", "error", "agents")}))

    def create(self):
        status, snapshot = self.request("/api/sessions", {"action": "create", "name": "native"})
        self.assertEqual(status, 201, snapshot)
        return snapshot["id"]

    def test_sub_resume_rejects_changed_code_and_preserves_history_for_clean_restart(self):
        identifier = self.create()
        self.wait(identifier, lambda item: item["master_status"] == "sleeping")
        master = self.app.get(identifier)
        child = master.team.request("master", "create_agent", {"name": "resume-fixture"})["agent_id"]
        def status(snapshot):
            return next(item["status"] for item in snapshot["agents"] if item["id"] == child)
        self.wait(identifier, lambda item: status(item) == "sleeping")
        runtime = master.team.agents[child]
        history = runtime.store.folder / "state.json"
        master.team.request("master", "stop_agent", {"agent_id": child, "reason": "resume regression"})
        self.wait(identifier, lambda item: status(item) == "stopped")
        before = history.read_bytes()
        with patch("multiagent.runtime_build.source_revision", return_value="changed-source"),\
                patch("multiagent.agent.subprocess.Popen") as spawn:
            result = master.team.request("master", "resume_agent", {"agent_id": child})
            self.assertIn(result["status"], {"stopping", "failed"})
            self.wait(identifier, lambda item: status(item) == "failed")
            spawn.assert_not_called()
        error = next(item["error"] for item in master.team.listing() if item["id"] == child)
        self.assertIn("代码版本不一致", error)
        self.assertEqual(history.read_bytes(), before, "Mixed builds cannot overwrite committed context")
        master.team.request("master", "resume_agent", {"agent_id": child})
        self.wait(identifier, lambda item: status(item) == "sleeping")
        after = json.loads(history.read_text())
        previous = json.loads(before)
        self.assertEqual(after["messages"][:len(previous["messages"])], previous["messages"])
        self.assertGreater(after["iteration"], previous["iteration"])



    def test_demo_directed_inbox_human_outbox_and_runtime_controls(self):
        identifier = self.create()
        base = f"/api/sessions/{identifier}"
        runtime = self.app.get(identifier)
        initial = self.wait(identifier, lambda item: item["master_status"] == "sleeping")
        self.assertEqual(initial["idle_sleep_seconds"], 10)
        self.assertEqual(self.request(base + "/idle-sleep", {"idle_sleep_seconds": .2})[0], 200)
        self.assertEqual(runtime.snapshot()["idle_sleep_seconds"], .2)
        for removed in ("turn", "reset", "broadcast"):
            self.assertEqual(self.request(base + "/" + removed, {"prompt": "removed channel"})[0], 404)
        queued = self.request(base + "/input", {"prompt": "Inspect workspace"})[1]
        reported = self.wait(identifier, lambda item: item["master_status"] == "sleeping" and
                             any(event["type"] == "human_message" for event in item["events"]))
        self.assertTrue(any(event["type"] == "user_input_applied" and event["message_id"] == queued["queued"] for event in reported["events"]))
        self.assertFalse(any(event["type"] in {"answer", "turn_start", "turn_end", "turn_error"} for event in reported["events"]))
        self.assertEqual(len({event["run_id"] for event in reported["events"] if event["type"] == "model_start"}), 1)
        worker = runtime.master.process
        self.assertTrue(self.request(base + "/cancel", {})[1]["accepted"])
        self.wait(identifier, lambda item: item["status"] == "stop")
        worker.wait(timeout=3)
        status, offline = self.request(base + "/input", {"prompt": "user input queued while stopped"})
        self.assertEqual(status, 403)
        stopped = runtime.snapshot()
        self.assertEqual(stopped["status"], "stop")
        self.assertEqual(self.request(base + "/continue", {})[0], 202)
        runtime = self.app.get(identifier)
        offline = self.request(base + "/input", {"prompt": "user input after Resume"})[1]
        resumed = self.wait(identifier, lambda item: item["master_status"] == "sleeping" and any(
            event["type"] == "user_input_applied" and event["message_id"] == offline["queued"] for event in item["events"]))
        self.assertTrue(any(event["type"] == "user_input_applied" and event["message_id"] == offline["queued"] for event in resumed["events"]))
        self.assertIsNot(runtime.master.process, worker)
        self.assertEqual(sum(event["type"] == "human_message" for event in resumed["events"]), 2)
        starts = [event["step"] for event in resumed["events"] if event["type"] == "model_start"]
        self.assertEqual(starts, sorted(set(starts)), "Iteration numbers persist across Resume")
        state = SessionStore(resumed["session_directory"]).load_state()
        self.assertNotIn("turn_number", state)
        self.assertEqual(state["iteration"], starts[-1])
        self.assertEqual(self.request(base + "/idle-sleep", {"idle_sleep_seconds": 0})[0], 400)

    @unittest.skipUnless(shutil.which("openssl"), "isolated TLS fixture requires openssl")
    def test_tls_plain_text_fallback_wake_and_display_vs_explicit_vision(self):
        cert, key = Path(self.temp.name) / "cert.pem", Path(self.temp.name) / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
                        "-keyout", str(key), "-out", str(cert)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        display_bytes, peer_bytes = png(4, 6), png(2, 3)
        (self.root / "display.png").write_bytes(display_bytes)
        (self.root / "peer.png").write_bytes(peer_bytes)
        entered, release = threading.Event(), threading.Event()
        lock = threading.Lock()
        fixture = {"master": 0, "sub": 0, "child": None, "requests": [], "errors": []}

        def envelopes(messages):
            for message in messages:
                content = message.get("content")
                if message["role"] != "user" or not isinstance(content, str):
                    continue
                content = content.removeprefix("<user_query>").removesuffix("</user_query>")
                try:
                    item = json.loads(content.split("\n", 2)[-1])
                except ValueError:
                    continue
                if isinstance(item, dict) and item.get("from"):
                    yield item

        def response(request):
            messages = request["messages"]
            identity = re.search(r"your agent_id is ([a-z0-9]+)", messages[0]["content"], re.I).group(1)
            role = "master" if identity == "master" else "sub"
            fixture["requests"].append((role, request))
            incoming = list(envelopes(messages))
            def call(name, fields, content=None):
                return {"role": "assistant", "content": content, "tool_calls": [{"index": 0, "id": f"fixture-{len(fixture['requests'])}",
                        "type": "function", "function": {"name": name, "arguments": json.dumps(fields)}}]}
            def sleep():
                return call("sleep", {"seconds": 300})
            if role == "master":
                stage = fixture[role]
                if stage == 0:
                    fixture[role] = 1
                    return {"role": "assistant", "content": "INTERNAL NOTE\n![display](/workspace/display.png)"}
                if stage == 1:
                    fixture[role] = 2
                    return call("create_agent", {"name": "vision-worker"}, "TOOL TEXT\n![display](/workspace/display.png)")
                if stage == 2:
                    results = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
                    fixture["child"] = next(item["agent_id"] for item in reversed(results) if "agent_id" in item)
                    fixture[role] = 3
                    return call("send_agent_message", {"agent_id": fixture["child"], "message": "Inspect the delivered peer figure and report explicitly.",
                                                       "images": ["/workspace/peer.png"]})
                if stage == 3 and any("VISION_RESULT" in str(item.get("text")) for item in incoming):
                    fixture[role] = 4
                    return call("send_user_message", {"message": "MASTER REPORT\n![display](/workspace/display.png)", "images": ["/workspace/display.png"]})
                if stage == 4 and "PING_NEW" in json.dumps(messages):
                    fixture[role] = 5
                    return call("send_user_message", {"message": "NEW_WAKE_ACK"})
                return sleep()
            if fixture[role] == 0:
                assignment = next((item for item in incoming if item.get("from") == "master" and item.get("images")), None)
                if assignment:
                    fixture[role] = 1
                    return call("view_image", {"path": assignment["images"][0]["path"]})
                return sleep()
            if fixture[role] == 1:
                fixture[role] = 2
                return call("send_agent_message", {"agent_id": "master", "message": "VISION_RESULT", "images": ["/workspace/peer.png"]})
            if fixture[role] == 2:
                fixture[role] = 3
                return call("send_user_message", {"message": "CHILD REPORT\n![peer](/workspace/peer.png)", "images": ["/workspace/peer.png"]})
            return sleep()

        class ModelHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if not fixture["requests"]:
                    entered.set()
                    release.wait(8)
                try:
                    with lock:
                        message = response(request)
                    assert request["tool_choice"] == "auto"
                    body = ("data: " + json.dumps({"model": "gemini-4-argon", "choices": [{"index": 0, "delta": message,
                            "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}]}) +
                            "\n\ndata: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}) +
                            "\n\ndata: [DONE]\n\n").encode()
                    self.send_response(200)
                    mime = "text/event-stream"
                except Exception as error:
                    fixture["errors"].append(repr(error))
                    body, mime = json.dumps({"error": str(error)}).encode(), "application/json"
                    self.send_response(400)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        tls = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        tls.socket = context.wrap_socket(tls.socket, server_side=True)
        thread = threading.Thread(target=tls.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        thread.start()
        self.addCleanup(tls.server_close)
        self.addCleanup(tls.shutdown)
        self.addCleanup(self.app.close)
        self.app.config["demo"] = False
        environment = {"GEMINI_API_KEY": "native-fixture-key", "SSL_CERT_FILE": str(cert),
                       "GEMINI_BASE_URL": f"https://localhost:{tls.server_port}/v1", "NO_PROXY": "localhost,127.0.0.1"}
        with patch.dict(os.environ, environment):
            identifier = self.create()
            base = f"/api/sessions/{identifier}"
            self.assertTrue(entered.wait(5))
            status, upload = self.request(base + "/uploads", data=png())
            self.assertEqual(status, 201, upload)
            self.assertEqual(self.request(base + "/input", {"prompt": "NATIVE_V4_TASK", "attachments": [upload["id"]]})[0], 202)
            release.set()
            result = self.wait(identifier, lambda item: item["master_status"] == "sleeping" and
                               any(event["type"] == "human_message" and "MASTER REPORT" in event["content"] for event in item["events"]), timeout=20)
            child_id = fixture["child"]
            self.wait(identifier, lambda _: any(event["type"] == "human_message" for event in self.app.agent_snapshot(identifier, child_id)["events"]))
            self.assertEqual(self.request(base + "/input", {"prompt": "PING_NEW"})[0], 202)
            self.wait(identifier, lambda item: any(event["type"] == "human_message" and event["content"] == "NEW_WAKE_ACK" for event in item["events"]))
        self.assertEqual(fixture["errors"], [])
        runtime = self.app.get(identifier)
        state = runtime.store.load_state()
        child = self.app.agent_snapshot(identifier, child_id)
        displayed = [event for event in result["events"] if event["type"] == "model_end" and event.get("images")]
        self.assertGreaterEqual(len(displayed), 2, "Plain text and text beside tool calls both retain display assets")
        (self.root / "display.png").unlink()
        image = displayed[0]["images"][0]
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request("GET", base + "/images/" + image["sha256"], headers={"X-recoding-Page":"test-page"})
        image_response = connection.getresponse()
        self.assertEqual(image_response.status, 200)
        self.assertEqual(image_response.read(), display_bytes)
        connection.close()
        self.assertEqual(result["context"]["images"], 1, "Only the human-pasted image enters Master's visual context")
        self.assertEqual(child["context"]["images"], 1, "Peer image enters child context only after explicit view_image")
        first_child = next(request for role, request in fixture["requests"] if role == "sub" and "Inspect the delivered" in json.dumps(request["messages"]))
        self.assertNotIn('"type": "image_url"', json.dumps(first_child["messages"]))
        self.assertTrue(any(role == "sub" and '"type": "image_url"' in json.dumps(request["messages"]) for role, request in fixture["requests"]))
        fallback = next(event for event in result["events"] if event["type"] == "tool_end" and event.get("name") == "sleep" and event.get("origin") == "system")
        self.assertLess(fallback["elapsed"], 2, "Preexisting mail immediately wakes the plain-response fallback sleep")
        self.assertFalse(any(event["type"] in {"answer", "turn_start", "turn_end", "turn_error"} for event in result["events"]))
        self.assertFalse(any(message.get("_image_attachments") for message in state["messages"] if message["role"] == "assistant"))
        self.assertFalse(any(key.startswith("_display_") for _, request in fixture["requests"] for message in request["messages"] for key in message))
        self.assertTrue(self.request(base + "/cancel", {})[1]["accepted"])
        self.wait(identifier, lambda item: item["master_status"] == "stopped" and all(agent["status"] == "stopped" for agent in item["agents"]))
        with patch.dict(os.environ, environment):
            self.assertEqual(self.request(base + "/continue", {})[0], 202)
            continued = self.wait(identifier, lambda item: item["master_status"] == "sleeping")
        self.assertTrue(all(agent["status"] == "stopped" for agent in continued["agents"]))


class PendingInputObservationTests(unittest.TestCase):
    def test_slow_constructor_does_not_publish_unsequenced_child_events(self):
        from multiagent import MultiAgentSystem
        team = SimpleNamespace(condition=threading.Condition(),agents={},
                               record=lambda _:{"status":"starting"},stream=Mock(),emit=Mock())
        message = {"id":"already-saved-mail","source":"user","target":"slow-worker","kind":"user_input", "published":False}
        with patch.object(team.condition,"wait_for",return_value=False):
            MultiAgentSystem._publish_message(team,message)
        team.stream.assert_not_called()
        team.emit.assert_not_called()
        self.assertFalse(message["published"])



if __name__ == "__main__":
    unittest.main()
