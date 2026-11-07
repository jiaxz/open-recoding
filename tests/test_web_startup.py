"""Focused subprocess checks for replacing a same-port Web server."""

import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import unittest

from workspace_support import temporary_workspace

from session.store import StoreLease


PROJECT = Path(__file__).resolve().parents[1]


class WebStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_workspace(prefix="agent-startup-")
        self.root = Path(self.temp.name)
        self.processes = []
        self.logs = {}
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GEMINI_")}
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for process in reversed(self.processes):
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        self.temp.cleanup()

    def free_port(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def launch(self, port, *extra, wait=True, command=None):
        log = self.root / f"process-{len(self.processes)}.log"
        with log.open("wb") as output:
            process = subprocess.Popen(command or [sys.executable, str(PROJECT / "recoding-web.py"), "--demo",
                "--env-file", str(self.root / ".unused-test-env"),
                "--no-network", "--no-gpu", "-C", str(self.root / "workspace"),
                "--tool-timeout", "60", "--port", str(port), *extra],
                cwd=PROJECT, env=self.env, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                start_new_session=True)
        self.processes.append(process)
        self.logs[process.pid] = log
        if wait:
            for _ in range(150):
                if process.poll() is not None:
                    self.fail(log.read_text())
                try:
                    if self.request(port, "/api/config")[1].get("pid") == process.pid:
                        return process
                except (OSError, http.client.HTTPException, ValueError):
                    pass
                time.sleep(0.05)
            self.fail("New Web server did not start: " + log.read_text())
        return process

    def request(self, port, path, body=None):
        headers = {"Content-Type": "application/json", "X-recoding-Page":"startup-test",
                   "X-recoding-Document":"startup-document"}
        if body is not None:
            headers["X-Agent-Token"] = self.request(port, "/api/config")[1]["csrf"]
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        try:
            client.request("GET" if body is None else "POST", path,
                           None if body is None else json.dumps(body), headers)
            response = client.getresponse()
            return response.status, json.loads(response.read())
        finally:
            client.close()

    def wait_session(self, port, identifier, predicate):
        for _ in range(100):
            status, snapshot = self.request(port, f"/api/sessions/{identifier}")
            if status == 200 and predicate(snapshot):
                return snapshot
            time.sleep(0.05)
        self.fail("Session did not reach the requested state")

    def test_default_replaces_server_cancels_task_and_preserves_archive(self):
        port = self.free_port()
        old = self.launch(port)
        old_config = self.request(port, "/api/config")[1]
        status, session = self.request(port, "/api/sessions", {"name": "saved-task"})
        self.assertEqual(status, 201)
        self.wait_session(port, session["id"], lambda item: item["master_status"] == "sleeping")
        marker = self.root / "workspace" / "keep.txt"
        marker.write_text("preserve workspace")
        queued = self.request(port, f'/api/sessions/{session["id"]}/input', {"prompt": "等待"})[1]
        self.wait_session(port, session["id"], lambda item: any(event["type"] == "user_input_applied" and
            event["message_id"] == queued["queued"] for event in item["events"]) and any(
            event["type"] == "tool_start" and event.get("name") == "bash" for event in item["events"]))
        new = self.launch(port)
        self.assertEqual(old.wait(timeout=3), 0)
        config = self.request(port, "/api/config")[1]
        self.assertEqual(config["service"], "recoding-web")
        self.assertEqual(config["pid"], new.pid)
        self.assertNotEqual(config["instance_id"], old_config["instance_id"])
        self.assertIn("Replacing Web server", self.logs[new.pid].read_text())
        self.assertEqual(marker.read_text(), "preserve workspace")
        folder = Path(session["session_directory"])
        self.assertTrue((folder / "state.json").is_file())
        lease = StoreLease(folder)
        lease.close()  # The old worker must release its archive before Resume.
        self.assertIn(session["id"], [item["id"] for item in self.request(port, "/api/sessions")[1]["sessions"]])
        self.request(port, "/api/sessions", {"action": "resume", "id": session["id"]})
        self.wait_session(port, session["id"], lambda item: item["master_status"] == "sleeping")
        new.terminate()
        self.assertEqual(new.wait(timeout=5), 0)
        lease = StoreLease(folder)
        lease.close()  # SIGTERM follows the same graceful runtime cleanup.

    def test_no_replace_leaves_existing_server_untouched(self):
        port = self.free_port()
        old = self.launch(port)
        refused = self.launch(port, "--no-replace", wait=False)
        self.assertEqual(refused.wait(timeout=5), 1)
        self.assertIsNone(old.poll())
        self.assertEqual(self.request(port, "/api/config")[1]["pid"], old.pid)

    def test_same_named_unrelated_web_script_is_not_terminated(self):
        port = self.free_port()
        script = self.root / "recoding-web.py"
        script.write_text("from http.server import HTTPServer, SimpleHTTPRequestHandler\n"
                          f"HTTPServer(('127.0.0.1', {port}), SimpleHTTPRequestHandler).serve_forever()\n")
        other = self.launch(port, wait=False, command=[sys.executable, str(script)])
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.02)
        else:
            self.fail("Unrelated fixture did not start")
        refused = self.launch(port, wait=False)
        self.assertEqual(refused.wait(timeout=5), 1)
        self.assertIsNone(other.poll())
        self.assertIn("其他程序占用", self.logs[refused.pid].read_text())


if __name__ == "__main__":
    unittest.main()
