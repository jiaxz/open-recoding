"""Focused startup diagnostics with blocked filesystem reads; no model API."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time
import unittest

from workspace_support import temporary_workspace
from unittest.mock import patch
from urllib.request import Request, urlopen

from session.store import SessionStore
from recoding_web.host import GuiSessionHost
from recoding_web.server import GuiHttpServer


class StartupProgressTests(unittest.TestCase):
    def test_live_progress_does_not_wait_for_scan_or_resume_lock(self):
        with temporary_workspace() as temp:
            saved = SessionStore.create(Path(temp), "Progress fixture", {})
            app = GuiSessionHost(temp, demo=True, network=False, gpu=False, defer_discovery=True)
            server = GuiHttpServer(("127.0.0.1", 0), app)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"

            def request(path, body=None, operation=None):
                headers = {"X-recoding-Page":"test-page","Content-Type": "application/json", "X-Agent-Token": server.token}
                if operation:
                    headers["X-recoding-Operation"] = operation
                data = json.dumps(body).encode() if body is not None else None
                with urlopen(Request(base + path, data=data, headers=headers), timeout=2) as response:
                    return json.load(response)

            entered, release = threading.Event(), threading.Event()
            original = app.catalog._directory_entries

            def blocked_scan(directory):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("Scan test gate was not released")
                return original(directory)

            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    with patch.object(app.catalog, "_directory_entries", side_effect=blocked_scan):
                        pending = pool.submit(request, "/api/sessions", operation="scan-test")
                        self.assertTrue(entered.wait(2))
                        # The catalog thread holds manager.lock throughout this read.
                        live = request("/api/progress?id=scan-test")["operation"]
                        self.assertEqual(live["status"], "running")
                        self.assertEqual(live["steps"][-1]["key"], "scan")
                        self.assertIn(temp, live["steps"][-1]["detail"])
                        release.set()
                        self.assertEqual(pending.result()["sessions"][0]["id"], saved.id)

                    entered.clear()
                    release.clear()
                    load_events = SessionStore.load_events

                    def blocked_history(store, *args, **kwargs):
                        entered.set()
                        if not release.wait(5):
                            raise TimeoutError("Resume test gate was not released")
                        return load_events(store, *args, **kwargs)

                    with patch.object(SessionStore, "load_events", blocked_history):
                        pending = pool.submit(request, "/api/sessions", {"action": "resume", "id": saved.id}, "resume-test")
                        self.assertTrue(entered.wait(2))
                        live = request("/api/progress?id=resume-test")["operation"]
                        self.assertEqual(live["steps"][-1]["key"], "history")
                        self.assertEqual(live["status"], "running")
                        release.set()
                        self.assertEqual(pending.result()["id"], saved.id)

                deadline = time.monotonic() + 5
                while app.sessions[saved.id].master.status == "starting" and time.monotonic() < deadline:
                    time.sleep(.02)
                finished = request("/api/progress?id=resume-test")["operation"]
                self.assertEqual(finished["status"], "done")
                self.assertEqual(finished["runtime"]["status"], "done", finished)
                self.assertTrue({"history", "rates", "agents", "snapshot", "response"}.issubset(
                    {step["key"] for step in finished["steps"]}))
                self.assertTrue({"state", "environment", "history", "restore"}.issubset(
                    {step["key"] for step in finished["runtime"]["steps"]}))
                self.assertTrue(all(step["elapsed_seconds"] >= 0 for step in finished["steps"]))
            finally:
                release.set()
                server.shutdown()
                server.server_close()
                thread.join(2)
                app.close()


if __name__ == "__main__":
    unittest.main()
