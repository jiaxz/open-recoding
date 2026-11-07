"""Local HTTP, real worker/sandbox, cancellation and session-lifecycle tests."""

import http.client
import json
import os
from pathlib import Path
import shutil
import threading
import time
import unittest

from workspace_support import temporary_workspace
import uuid
from unittest.mock import patch

from recoding_web.host import GuiSessionHost
from recoding_web.server import GuiHttpServer
from session.store import SESSION_HOME, SessionStore
from test_images import png


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_workspace(prefix="agent-web-test-")
        self.root = Path(self.temp.name) / "sessions"
        self.page = str(uuid.uuid4())
        self.app = GuiSessionHost(self.root, demo=True, network=False, gpu=False, tool_timeout=5)
        self.server = GuiHttpServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.app.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, body=None, *, headers=None, token=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        values = {"X-recoding-Page":self.page, "Content-Type": "application/json"}
        if token:
            values["X-Agent-Token"] = self.server.token
        values.update(headers or {})
        connection.request("GET" if body is None else "POST", path,
                           body=None if body is None else json.dumps(body), headers=values)
        response = connection.getresponse()
        raw = response.read()
        status, content_type = response.status, response.getheader("Content-Type", "")
        connection.close()
        return status, json.loads(raw) if "application/json" in content_type else raw.decode()

    def binary(self, path, data=None, *, name='截图.png', token=True, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        from urllib.parse import quote
        values = {'X-recoding-Page':self.page,'Content-Type': 'image/png', 'X-File-Name': quote(name)}
        if token:
            values['X-Agent-Token'] = self.server.token
        values.update(headers or {})
        connection.request('GET' if data is None else 'POST', path, body=data, headers=values)
        response = connection.getresponse()
        raw, status, mime = response.read(), response.status, response.getheader('Content-Type', '')
        connection.close()
        return status, json.loads(raw) if 'application/json' in mime else raw

    def wait(self, name, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = self.app.get(name).snapshot()
            if predicate(snapshot):
                return snapshot
            time.sleep(0.01)
        self.fail("Web session did not reach the expected state: " + repr({key:snapshot.get(key) for key in ('status','master_status','error','read_only')}))

    def open(self, name="first"):
        status, snapshot = self.request("/api/sessions", {"name": name})
        self.assertEqual(status, 201, snapshot)
        return self.wait(name, lambda item: item["master_status"] == "sleeping")

    def queue_input(self, name, prompt, attachments=()):
        before = self.app.get(name).sequence
        status, queued = self.request(f"/api/sessions/{name}/input", {"prompt": prompt, "attachments": list(attachments)})
        self.assertEqual(status, 202, queued)
        self.assertTrue(queued["queued"])
        return {**queued, "after": before}

    @staticmethod
    def applied(snapshot, queued):
        return next((event for event in snapshot["events"] if event["type"] == "user_input_applied" and
                     event["message_id"] == queued["queued"] and event["sequence"] > queued["after"]), None)

    def run_input(self, name, prompt, attachments=()):
        queued = self.queue_input(name, prompt, attachments)
        def reported(item):
            applied = self.applied(item, queued)
            return item["master_status"] == "sleeping" and applied and any(event["type"] == "human_message" and
                event["sequence"] > applied["sequence"] for event in item["events"])
        return self.wait(name, reported)

    def wait_python(self, name, queued):
        def executing(item):
            applied = self.applied(item, queued)
            return applied and any(event["type"] == "tool_start" and event.get("name") == "bash" and
                                  event["sequence"] > applied["sequence"] for event in item["events"])
        return self.wait(name, executing)

    def test_assets_configuration_and_secret_never_reaches_browser(self):
        status, config = self.request("/api/config")
        self.assertEqual(status, 200)
        self.assertTrue(config["demo"])
        self.assertNotIn("api_key", config)
        for path, expected in [("/", "recoding"), ("/app.js", "EventSource"), ("/styles.css", "prefers-reduced-motion")]:
            status, content = self.request(path)
            self.assertEqual(status, 200)
            self.assertIn(expected, content)
        self.assertEqual(self.request("/../pyproject.toml")[0], 404)

    def test_new_sessions_default_to_grok_even_with_an_existing_environment_model(self):
        with patch.dict(os.environ, {"GEMINI_MODEL": "gemini-4-argon"}), patch.dict(self.app.config, {"demo": False}):
            self.assertEqual(self.server.configuration()["model"], "grok-4.7")
            # A constructor substitute prevents any worker or external request.
            with patch("recoding_web.host.SessionHostProtocol.resume") as worker:
                worker.return_value.status = "run"
                worker.return_value.closed = False
                self.app.open("default-model", action="create")
                first = SessionStore(worker.call_args.args[0])
                self.assertEqual(first.settings()["model"], "grok-4.7")
                self.app.open("chosen-model", action="create", model="gemini-3.8-flash")
                chosen = SessionStore(worker.call_args.args[0])
                self.assertEqual(chosen.settings()["model"], "gemini-3.8-flash")
                self.app.open(identifier=chosen.id, action="resume")
                self.assertEqual(chosen.settings()["model"], "gemini-3.8-flash")
                self.assertEqual(worker.call_count, 2, "Resume keeps an already opened Session and its selected model")

    def test_deferred_discovery_keeps_page_available_while_first_catalog_scans(self):
        fixture = SessionStore.create(self.root / "project", "saved-session", {})
        self.app.close()
        self.app = GuiSessionHost(self.root, demo=True, network=False, gpu=False, defer_discovery=True)
        self.server.host = self.app
        self.assertFalse(self.app.catalog.discovery_complete)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        original = self.app.catalog.refresh
        result = []

        def slow_scan(progress=None):
            entered.set()
            release.wait(3)
            original(progress)

        def catalog():
            try:
                result.append(self.request("/api/sessions"))
            finally:
                finished.set()

        with patch.object(self.app.catalog, "refresh", side_effect=slow_scan):
            request = threading.Thread(target=catalog)
            request.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.request("/")[0], 200)
                status, config = self.request("/api/config")
                self.assertEqual(status, 200)
                self.assertEqual(config["session_root"], str(self.root))
                self.assertFalse(finished.is_set(), "HTML/config must be usable before discovery finishes")
            finally:
                release.set()
                request.join(timeout=5)
        self.assertEqual(result[0][0], 200)
        self.assertIn(fixture.id, {item["id"] for item in result[0][1]["sessions"]})
        self.assertTrue(self.app.catalog.discovery_complete)

    def test_context_summary_tracks_live_messages_images_resume_and_stop(self):
        from multiagent.multimodal import context_stats
        first = self.open("context-display")

        def check(snapshot):
            messages = SessionStore(snapshot["session_directory"]).load_state()["messages"]
            for key, value in context_stats(messages).items():
                self.assertEqual(snapshot["context"][key], value)
            self.assertIsNone(snapshot["context"]["last_input_tokens"], "Demo has no API token usage")

        check(first)
        self.assertGreaterEqual(first["context"]["messages"], 2)
        status, picture = self.binary(f'/api/sessions/{first["id"]}/uploads', png())
        self.assertEqual(status, 201)
        answer = self.run_input(first["id"], "检查目录", [picture["id"]])
        check(answer)
        self.assertEqual(answer["context"]["images"], 1)
        self.assertGreater(answer["context"]["context_chars"], first["context"]["context_chars"])
        self.app.close_session(first["id"])
        self.app.open(identifier=first["id"], action="resume")
        restored = self.wait(first["id"], lambda item: item["master_status"] == "sleeping")
        check(restored)
        self.assertEqual(restored["context"]["images"], 1)
        queued = self.queue_input(first["id"], "等待")
        self.wait_python(first["id"], queued)
        self.request(f'/api/sessions/{first["id"]}/cancel', {})
        cancelled = self.wait(first["id"], lambda item: item["status"] == "stop")
        check(cancelled)
        self.assertEqual(cancelled["context"]["images"], 1)
        self.assertEqual(self.request(f'/api/sessions/{first["id"]}/reset', {})[0], 403)
        self.assertEqual(self.open("new-context")["context"]["images"], 0)

    def test_loop_lifecycle_persists_human_messages_and_stop(self):
        first = self.open("lifecycle")
        self.assertNotIn("last_task", first)
        reported = self.run_input(first["id"], "检查当前目录")
        report = next(event for event in reported["events"] if event["type"] == "human_message")
        self.assertEqual(report["source"], "master")
        self.app.close_session(first["id"])
        self.app.open(identifier=first["id"], action="resume")
        restored = self.wait(first["id"], lambda item: item["master_status"] == "sleeping")
        self.assertTrue(any(event["type"] == "human_message" and event["message_id"] == report["message_id"] for event in restored["events"]))
        queued = self.queue_input(first["id"], "等待")
        self.wait_python(first["id"], queued)
        self.request(f'/api/sessions/{first["id"]}/cancel', {})
        stopped = self.wait(first["id"], lambda item: item["status"] == "stop")
        self.assertTrue(any(event["type"] == "loop_end" for event in stopped["events"]))

    def test_session_catalog_only_exposes_session_directories_and_tracks_external_sessions(self):
        ordinary = self.root / "ordinary"
        ordinary.mkdir()
        (ordinary / "notes.md").write_text("ordinary workspace file")
        empty = self.request("/api/sessions")[1]
        self.assertEqual(empty["workspaces"], [str(self.root)])
        self.assertEqual(empty["sessions"], [])
        self.assertNotIn("filesystem", empty)
        first = self.open("session-manager")
        runtime = self.app.get(first["id"]).master.process
        external = Path(self.temp.name) / "external"
        status, opened = self.request("/api/sessions", {"action": "create", "name": "external", "directory": str(external)})
        self.assertEqual(status, 201)
        self.wait(opened["id"], lambda item: item["master_status"] == "sleeping")
        self.app.close_session(opened["id"])
        added = SessionStore.create(external, "externally-added", {})
        nested = SessionStore.create(self.root / "projects" / "nested", "nested", {})
        status, listing = self.request("/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(set(listing["workspaces"]), {str(self.root), str(external), str(nested.workspace)})
        self.assertIn(added.id, {item["id"] for item in listing["sessions"]})
        self.assertNotIn("notes.md", json.dumps(listing))
        shutil.rmtree(added.folder)
        shutil.rmtree(opened["session_directory"])
        shutil.rmtree(nested.folder)
        status, changed = self.request("/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(changed["workspaces"], [str(self.root)])
        self.assertIsNone(runtime.poll())
        # An empty external workspace stays watched for this server run, even
        # though it has no directory row until another Session appears there.
        reappeared = SessionStore.create(external, "reappeared", {})
        self.assertIn(reappeared.id, {item["id"] for item in self.request("/api/sessions")[1]["sessions"]})

    def test_navigation_survives_removed_active_and_saved_session_archives(self):
        active = self.open("active-archive")
        saved = self.open("saved-archive")
        self.app.close_session(saved["id"])
        runtime = self.app.get(active["id"]).master.process
        shutil.rmtree(active["session_directory"])
        shutil.rmtree(saved["session_directory"])
        status, listing = self.request("/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(listing["sessions"], [], "The sidebar follows archives that still exist")
        self.assertIsNone(runtime.poll())
        status, snapshot = self.request(f'/api/sessions/{active["id"]}')
        self.assertEqual(status, 200)
        self.assertTrue(snapshot['storage_missing'])
        self.app.close_session(active["id"])
        self.assertFalse(Path(active["session_directory"]).exists())
        self.assertEqual(self.request("/api/sessions")[1]["sessions"], [])

    def test_frontend_is_frozen_with_backend_instance_until_server_restart(self):
        config = self.request('/api/config')[1]
        self.assertEqual(config['api_version'], 4)
        self.assertTrue(config['capabilities']['session_delete'])
        self.assertEqual(len(config['frontend_revision']), 16)
        original = self.request('/app.js')[1]
        assets = Path(self.temp.name)/'edited-assets'
        assets.mkdir()
        (assets/'app.js').write_text('new-frontend-build')
        with patch('recoding_web.server.STATIC', assets):
            # A running handler keeps its matching UI even when files change.
            self.assertEqual(self.request('/app.js')[1], original)
            with patch('recoding_web.server.ASSETS', {'/app.js': ('app.js','text/javascript')}):
                newer = GuiHttpServer(("127.0.0.1", 0), self.app)
            try:
                self.assertEqual(newer.assets['/app.js'][0], b'new-frontend-build')
                self.assertNotEqual(newer.instance_id, config['instance_id'])
                self.assertNotEqual(newer.frontend_revision, config['frontend_revision'])
            finally:
                newer.server_close()

    def test_missing_delete_route_and_missing_session_have_distinct_error_codes(self):
        first = self.open()
        route_status, route = self.request(f'/api/sessions/{first["id"]}/unknown-action', {})
        self.assertEqual(route_status, 404)
        self.assertEqual(route['code'], 'route_not_found')
        self.assertIn('重启服务器', route['error'])
        session_status, missing = self.request('/api/sessions/missing-session/delete', {})
        self.assertEqual(session_status, 404)
        self.assertEqual(missing['code'], 'session_not_found')
        self.assertIn('当前服务器', missing['error'])
        self.assertEqual(missing['instance_id'], self.server.instance_id)

    def test_model_config_file_protection_is_runtime_only_and_reapplied_on_resume(self):
        secret = self.root / "custom-model.env"
        secret.write_text("GEMINI_API_KEY=local-fixture-only\n")
        self.app.close()
        self.app = GuiSessionHost(self.root, demo=True, network=False, gpu=False, private_files=[secret])
        self.server.host = self.app
        opened = self.open("private-config")
        session = self.app.get(opened["id"])
        self.assertIn(str(secret), session.config["private_files"])
        self.assertNotIn("private_files", self.server.configuration()["defaults"])
        manifest = json.loads((Path(opened["session_directory"]) / "session.json").read_text())
        self.assertNotIn("private_files", manifest["settings"])
        self.assertNotIn("local-fixture-only", json.dumps(manifest))
        self.app.close_session(opened["id"])
        self.app.open(identifier=opened["id"], action="resume")
        self.wait(opened["id"], lambda item: item["master_status"] == "sleeping")
        self.assertIn(str(secret), self.app.get(opened["id"]).config["private_files"])

    def test_csrf_origin_host_and_session_directory_boundaries(self):
        self.assertEqual(self.request("/api/sessions", {"name": "bad"}, token=False)[0], 403)
        self.assertEqual(self.request("/api/sessions", {"name": "bad"}, headers={"Origin": "https://other.example"})[0], 403)
        self.assertEqual(self.request("/api/config", headers={"Host": "other.example"})[0], 403)
        for name in ("../outside", "a/b", ".", ".."):
            self.assertEqual(self.request("/api/sessions", {"name": name})[0], 400)
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.root / "escape").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.request("/api/sessions", {"action": "resume", "path": str(self.root / "escape")})[0], 400)
        self.assertEqual(self.request("/api/sessions", {"name": "safe", "gpu": "false"})[0], 400)
        self.assertFalse((self.root / "safe").exists())

    def test_real_worker_persists_state_and_session_isolation_and_close(self):
        self.open("first")
        first = self.run_input("first", "hello")
        self.assertIn('"run": 1', next(event["content"] for event in first["events"] if event["type"] == "human_message"))
        again = self.run_input("first", "again")
        answers = [event["content"] for event in again["events"] if event["type"] == "human_message"]
        self.assertIn('"run": 1', answers[-1])
        self.open("second")
        second = self.run_input("second", "hello")
        self.assertIn('"run": 1', next(event["content"] for event in second["events"] if event["type"] == "human_message"))
        marker = self.root / "keep.txt"
        marker.write_text("keep")
        self.assertEqual(self.request("/api/sessions/first/reset", {})[0], 404)
        self.open("fresh-context")
        fresh = self.run_input("fresh-context", "new independent context")
        self.assertIn('"run": 1', [event["content"] for event in fresh["events"] if event["type"] == "human_message"][-1])
        self.assertEqual(self.request("/api/sessions/first/close", {})[0], 200)
        self.assertEqual(marker.read_text(), "keep")
        self.assertEqual(self.app.get("second").snapshot()["master_status"], "sleeping")

    def test_uploaded_image_and_resume_preserve_real_attachment_and_preview(self):
        first = self.open()
        status, picture = self.binary(f'/api/sessions/{first["id"]}/uploads', png())
        self.assertEqual(status, 201, picture)
        self.assertEqual(picture['name'], '截图.png')
        result = self.run_input(first['id'], '', [picture['id']])
        applied = next(event for event in result['events'] if event['type'] == 'user_input_applied')
        self.assertTrue(all(event['images'] == 1 for event in result['events']
                            if event['type'] == 'model_start' and event['sequence'] > applied['sequence']))
        self.assertNotIn('base64', json.dumps(result))
        record = SessionStore(result['session_directory']).load_state()
        self.assertTrue(any(message.get('_image_attachments') for message in record['messages']))
        shutil.rmtree(self.root / '.recoding' / 'inputs')
        self.restart_app()
        self.request('/api/sessions', {'action': 'resume', 'id': first['id']})
        resumed = self.wait(first['id'], lambda item: item['master_status'] == 'sleeping')
        self.assertTrue(any(event.get('attachments') for event in resumed['events']))
        self.assertEqual(self.binary(f'/api/sessions/{first["id"]}/images/{picture["sha256"]}'), (200, png()))
        continued = self.run_input(first['id'], '继续分析之前的图片')
        self.assertEqual([event['images'] for event in continued['events'] if event['type'] == 'model_start'][-1], 1)

    def test_image_routes_reject_nonimages_escape_private_paths_and_untrusted_uploads(self):
        first = self.open()
        prefix = f'/api/sessions/{first["id"]}'
        self.assertEqual(self.binary(prefix+'/uploads', png(), token=False)[0], 403)
        self.assertEqual(self.binary(prefix+'/uploads', b'not an image')[0], 400)
        self.assertEqual(self.binary(prefix+'/uploads', png(), headers={'Origin': 'https://other.example'})[0], 403)
        image = self.root/'chart.png'
        image.write_bytes(png())
        self.assertEqual(self.binary(prefix+'/workspace-image?path=chart.png'), (200, png()))
        (self.root/'leak.png').symlink_to(image)
        for path in ('../chart.png', '/etc/passwd', 'leak.png', '.recoding/x.png'):
            from urllib.parse import quote
            self.assertIn(self.binary(prefix+'/workspace-image?path='+quote(path))[0], (400, 403))
        self.assertEqual(self.binary(prefix+'/images/not-a-digest')[0], 400)
        self.assertEqual(self.request(prefix+'/input', {'prompt':'', 'attachments':['x'*32]})[0], 404)
        self.assertEqual(self.app.get(first['id']).master.status, 'sleeping')

    def test_rename_and_pin_saved_sessions_persist_without_starting_workers(self):
        first = self.open()
        self.run_input(first['id'], 'warmup')
        self.assertEqual(self.request(f'/api/sessions/{first["id"]}/metadata', {'name':'中文 新名称','pinned':True})[0], 200)
        self.assertEqual(self.app.get(first['id']).name, '中文 新名称')
        self.app.close_session(first['id'])
        self.restart_app()
        listing = self.request('/api/sessions')[1]['sessions']
        saved = next(item for item in listing if item['id']==first['id'])
        self.assertEqual(saved['name'], '中文 新名称')
        self.assertTrue(saved['pinned'])
        self.assertEqual(len(self.app.sessions), 0)
        self.assertEqual(self.request(f'/api/sessions/{first["id"]}/metadata', {'pinned':False})[0], 403)
        self.assertEqual(len(self.app.sessions), 0)
        self.assertEqual(self.request(f'/api/sessions/{first["id"]}/metadata', {'name':'../bad'})[0], 400)
        self.assertEqual(self.request(f'/api/sessions/{first["id"]}/metadata', {'pinned':'yes'})[0], 400)
        self.request('/api/sessions', {'action':'resume','id':first['id']})
        self.wait(first['id'], lambda item: item['master_status']=='sleeping')
        result = self.run_input(first['id'], 'after resume')
        self.assertIn('"run": 1', [event['content'] for event in result['events'] if event['type']=='human_message'][-1])

    def test_delete_running_session_stops_worker_and_preserves_workspace_and_other_sessions(self):
        first = self.open('delete-first')
        other = self.open('keep-second')
        marker = self.root/'keep.txt'
        marker.write_text('workspace file')
        package = self.root/'.python-packages'
        package.mkdir()
        (package/'keep.py').write_text('VALUE=42')
        queued = self.queue_input(first['id'], '等待')
        self.wait_python(first['id'], queued)
        process = self.app.get(first['id']).master.process
        status, result = self.request(f'/api/sessions/{first["id"]}/delete', {})
        self.assertEqual(status, 200, result)
        self.assertEqual(result['deleted'], first['id'])
        self.assertIsNotNone(process.poll())
        self.assertFalse(Path(first['session_directory']).exists())
        self.assertEqual(marker.read_text(), 'workspace file')
        self.assertTrue((package/'keep.py').exists())
        self.assertTrue(Path(other['session_directory']).exists())
        self.assertEqual(self.app.get(other['id']).master.status, 'sleeping')
        self.assertFalse(any(item['id']==first['id'] for item in self.request('/api/sessions')[1]['sessions']))
        self.assertNotIn(first['session_directory'], (self.root/SESSION_HOME/'registry.json').read_text())
        self.assertEqual(self.request('/api/sessions', {'action':'resume','id':first['id']})[0], 404)

    def test_delete_saved_session_respects_another_runtime_lock(self):
        from session.store import StoreLease
        first = self.open()
        self.app.close_session(first['id'])
        lease = StoreLease(first['session_directory'])
        try:
            self.assertEqual(self.request(f'/api/sessions/{first["id"]}/delete', {})[0], 403)
            self.assertEqual(self.request('/api/sessions', {'action':'resume','id':first['id']})[0], 400)
            self.assertTrue(Path(first['session_directory']).exists())
        finally:
            lease.close()
        self.assertEqual(self.request('/api/sessions', {'action':'resume','id':first['id']})[0], 201)
        self.wait(first['id'], lambda item:item['master_status']=='sleeping')
        self.assertEqual(self.request(f'/api/sessions/{first["id"]}/delete', {})[0], 200)
        self.assertFalse(Path(first['session_directory']).exists())

    def test_stop_current_python_step_retains_context_and_resume_rebuilds_runtime(self):
        self.open()
        self.run_input("first", "warm up")
        queued = self.queue_input("first", "等待")
        self.wait_python("first", queued)
        worker = self.app.get("first").master.process
        self.assertTrue(self.queue_input("first", "concurrent")["queued"])
        status, cancelled = self.request("/api/sessions/first/cancel", {})
        self.assertEqual(status, 200)
        self.assertTrue(cancelled["accepted"])
        snapshot = self.wait("first", lambda item: item["status"] == "stop")
        worker.wait(timeout=3)
        record = next(event["cancellation"] for event in snapshot["events"] if event["type"] == "loop_error")
        self.assertEqual(record["reason"], "cancelled")
        history = '\n'.join(path.read_text() for path in Path(snapshot['session_directory']).joinpath('runs').glob('*.jsonl'))
        self.assertIn("[Agent停止记录]", history)
        self.assertIn("warm up", history)
        self.assertEqual(self.request("/api/sessions/first/continue", {})[0], 202)
        recovered = self.run_input("first", "continue with a new task")
        self.assertIn('"run": 1', [event["content"] for event in recovered["events"] if event["type"] == "human_message"][-1])

    def test_stop_closes_subscription_and_resume_replays_cursor(self):
        ready = self.open()
        queued = self.queue_input("first", "等待")
        self.assertEqual(self.request("/api/sessions/first/cancel", {})[0], 200)
        stopped = self.wait("first", lambda item: item["status"] == "stop")
        self.assertTrue(any(event["type"] == "loop_stopped" for event in stopped["events"]))
        path = f"/api/sessions/first/events?after={ready['sequence']}&page={self.page}"
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            self.assertEqual(response.status, 204, "A stopped archive has no live SSE subscription")
            response.read()
            self.assertEqual(self.request("/api/sessions/first/continue", {})[0], 202)
            connection.request("GET", path)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("text/event-stream", response.getheader("Content-Type"))
            received = []
            while not any(event["type"] == "loop_started" for event in received):
                line = response.readline().decode()
                self.assertTrue(line)
                if line.startswith("data: "):
                    received.append(json.loads(line[6:]))
            self.assertTrue(all(event["sequence"] > ready["sequence"] for event in received))
            self.assertTrue(any(event.get("message_id") == queued["queued"] for event in received))
            response.close()
        finally:
            connection.close()

    def restart_app(self):
        self.app.close()
        self.app = GuiSessionHost(self.root, demo=True, network=False, gpu=False, tool_timeout=5)
        self.server.host = self.app

    def test_resume_after_server_restart_restores_context_events_settings_but_not_memory(self):
        first = self.open()
        old = self.run_input("first", "保留这次对话")
        folder = Path(old["session_directory"])
        self.assertEqual(folder.parent, self.root / SESSION_HOME)
        previous_python_calls = sum(event["type"] == "tool_start" and event.get("name") == "bash" for event in old["events"])
        self.request("/api/sessions/first/cancel", {})
        self.wait("first", lambda item: item["status"] == "stop")
        self.request("/api/sessions/first/continue", {})
        sleeping = self.wait("first", lambda item: item["master_status"] == "sleeping" and item.get("sleep", {}).get("seconds") == 300)
        self.assertEqual(sum(event["type"] == "tool_start" and event.get("name") == "bash" for event in sleeping["events"]),
                         previous_python_calls, "Resume without new mail must not replay the demo Python task")
        previous_events = len(old["events"])
        previous_iteration = SessionStore(folder).load_state()["iteration"]
        previous_runs = len(list((folder / "runs").glob("context_*.jsonl")))
        self.restart_app()
        listing = self.request("/api/sessions")[1]["sessions"]
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["id"], first["id"])
        self.assertEqual(listing[0]["status"], "stop")
        status, resumed = self.request("/api/sessions", {"action": "resume", "id": first["id"]})
        self.assertEqual(status, 201, resumed)
        self.wait(first["id"], lambda value: value["master_status"] == "sleeping")
        result = self.run_input(first["id"], "继续")
        answers = [event["content"] for event in result["events"] if event["type"] == "human_message"]
        self.assertEqual(len(answers), 2)
        self.assertIn('"run": 1', answers[-1])
        state = SessionStore(folder).load_state()
        self.assertGreater(state["iteration"], previous_iteration)
        self.assertIn("保留这次对话", json.dumps(state, ensure_ascii=False))
        self.assertIn("context_resume", Path(result["history"]).read_text())
        self.assertGreater(len(result["events"]), previous_events)
        self.assertEqual(len(list((folder / "runs").glob("context_*.jsonl"))), previous_runs + 1)

    def test_moving_archive_rebinds_workspace_and_discovery_keeps_identity(self):
        first = self.open()
        (self.root / "original-only.txt").write_text("old workspace")
        original = self.run_input("first", "查看旧目录")
        self.request("/api/sessions/first/close", {})
        workspace = self.root / "nested" / "new-workspace"
        (workspace / SESSION_HOME).mkdir(parents=True)
        (workspace / "new-only.txt").write_text("new workspace")
        destination = workspace / SESSION_HOME / "moved"
        shutil.move(original["session_directory"], destination)
        found = self.request("/api/sessions")[1]["sessions"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["id"], first["id"])
        self.assertEqual(found[0]["directory"], str(workspace))
        self.request("/api/sessions", {"action": "resume", "path": str(destination)})
        self.wait(first["id"], lambda item: item["master_status"] == "sleeping")
        result = self.run_input(first["id"], "查看移动后的目录")
        answer = [event["content"] for event in result["events"] if event["type"] == "human_message"][-1]
        self.assertIn("new-only.txt", answer)
        self.assertNotIn("original-only.txt", answer)
        self.assertIn("查看旧目录", json.dumps(SessionStore(destination).load_state(), ensure_ascii=False))

    def test_create_in_arbitrary_directory_and_resume_external_archive_after_restart(self):
        workspace = Path(self.temp.name) / "external" / "project"
        status, snapshot = self.request("/api/sessions", {"action": "create", "name": "外部目录", "directory": str(workspace)})
        self.assertEqual(status, 201, snapshot)
        self.wait(snapshot["id"], lambda item: item["master_status"] == "sleeping")
        self.run_input(snapshot["id"], "外部目录任务")
        self.restart_app()
        stored = self.request("/api/sessions")[1]["sessions"]
        self.assertEqual(stored, [], "Discovery remains scoped to the selected workspace root")
        status, resumed = self.request('/api/sessions', {'action': 'resume', 'path': snapshot['session_directory']})
        self.assertEqual(status, 201, resumed)
        self.assertEqual(resumed['directory'], str(workspace))
        self.assertEqual(resumed['id'], snapshot['id'])
        picker = self.request("/api/directories?path=" + str(workspace.parent))[1]
        self.assertEqual(picker["entries"][0]["path"], str(workspace))

    def test_worker_crash_keeps_committed_steps_and_resume_does_not_replay_sleep(self):
        first = self.open()
        self.run_input("first", "已完成的步骤")
        queued = self.queue_input("first", "等待")
        self.wait_python("first", queued)
        process = self.app.get("first").master.process
        process.kill()
        process.wait(timeout=5)
        self.wait("first", lambda snapshot: snapshot["master_status"] == "failed")
        self.restart_app()
        self.request("/api/sessions", {"action": "resume", "id": first["id"]})
        ready = self.wait(first["id"], lambda snapshot: snapshot["master_status"] == "sleeping")
        state = SessionStore(ready["session_directory"]).load_state()
        self.assertTrue(any(message["role"] == "tool" and "interrupted" in message["content"] for message in state["messages"]))
        self.assertIn("已完成的步骤", json.dumps(state, ensure_ascii=False))
        self.assertTrue(any(event["type"] == "loop_stopped" and event.get("status") == "failed" for event in ready["events"]))
        result = self.run_input(first["id"], "开始一个新任务")
        self.assertEqual(result["master_status"], "sleeping")



if __name__ == "__main__":
    unittest.main()
