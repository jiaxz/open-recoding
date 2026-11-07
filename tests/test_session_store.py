"""Portable state, immutable image assets, locking and core restore contracts."""

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import unittest

from workspace_support import temporary_workspace
from unittest.mock import Mock, patch

from sandbox_support import tool_sandbox
from sandbox_support import Sandbox
from multiagent.media import AgentMedia

from session.store import SESSION_HOME, StoreLease, SessionStore
from multiagent.loop import AgentLoop as Agent
from endless_support import drive
from multiagent.multimodal import make_tool_message, make_user_message, to_api_messages
from test_images import call, image_result, png


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_workspace()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def create(self):
        return SessionStore.create(self.root / "project", "测试 Session", {
            "network": False, "gpu": False, "tool_timeout": 5, "api_timeout": 30,
            "model": "fixture", "base_url": "https://example.test/v1", "api_key": "must-not-be-saved"})

    def test_context_images_settings_are_portable_and_snapshots_are_immutable(self):
        store = self.create()
        image = image_result()
        tool, _ = make_tool_message([], "image", image)
        messages = [{"role": "system", "content": "system"},
                    {"role": "assistant", "tool_calls": [call("image")]}, tool,
                    {"role": "assistant", "content": "图中是红色。"}]
        state = {"messages": messages, "iteration": 3, "tool_count": 1, "run_state": "stopped"}
        store.save_state(state)
        self.assertEqual(state["messages"], messages)
        saved = (store.folder / "state.json").read_text()
        self.assertNotIn("data:image", saved)
        self.assertEqual(store.load_state()["messages"], messages)
        self.assertNotIn("must-not-be-saved", (store.folder / "session.json").read_text())
        destination = self.root / "other" / SESSION_HOME / "moved-session"
        destination.parent.mkdir(parents=True)
        shutil.move(store.folder, destination)
        moved = SessionStore(destination)
        self.assertEqual(moved.id, store.id)
        self.assertEqual(moved.workspace, self.root / "other")
        self.assertEqual(moved.load_state()["messages"], messages)
        self.assertEqual(moved.settings()["tool_timeout"], 5)
        self.assertEqual((destination / "state.json").stat().st_mode & 0o777, 0o600)

    def test_missing_or_corrupt_image_refuses_resume_instead_of_dropping_history(self):
        store = self.create()
        tool, _ = make_tool_message([], "image", image_result())
        store.save_state({"messages": [{"role": "system", "content": "system"}, tool], "iteration": 1, "tool_count": 1})
        asset = next((store.folder / "images").iterdir())
        asset.write_bytes(b"broken")
        with self.assertRaisesRegex(ValueError, "损坏"):
            store.load_state()

    def test_uploaded_image_and_metadata_survive_archive_move_without_original_file(self):
        store = self.create()
        uploaded = store.add_upload(png(), '截图.png')
        self.assertEqual((store.workspace / '.recoding' / 'inputs' / '.gitignore').read_text(), '*\n')
        picture = store.upload_attachment(uploaded['id'])
        message = make_user_message([], '分析截图', [picture])
        store.save_state({'messages': [{'role': 'system', 'content': 'test'}, message]})
        store.update_metadata(name='重命名 Session', pinned=True)
        destination = self.root / 'new-project' / SESSION_HOME / 'moved'
        destination.parent.mkdir(parents=True)
        shutil.move(store.folder, destination)
        shutil.rmtree(store.workspace)
        moved = SessionStore(destination)
        self.assertEqual(moved.id, store.id)
        self.assertEqual(moved.name, '重命名 Session')
        self.assertTrue(moved.info()['pinned'])
        self.assertEqual(moved.load_state()['messages'][1], message)
        self.assertEqual(moved.image_asset(uploaded['sha256'])[0], png())
        self.assertEqual(moved.upload_attachment(uploaded['id'])['data_url'], picture['data_url'])

    def test_upload_cannot_write_through_workspace_symlink(self):
        store = self.create()
        outside = self.root / 'outside'
        outside.mkdir()
        (store.workspace / '.recoding' / 'inputs').symlink_to(outside, target_is_directory=True)
        with self.assertRaises((OSError, ValueError)):
            store.add_upload(png(), 'picture.png')
        self.assertEqual(list(outside.iterdir()), [])

    def test_workspace_inputs_migrate_and_remain_visible_without_exposing_session_data(self):
        store = self.create()
        inputs = store.workspace / '.recoding' / 'inputs'
        inputs.mkdir()
        (inputs / 'kept.png').write_bytes(png())
        legacy = store.workspace / '.agent-inputs'
        legacy.mkdir()
        (legacy / 'old.png').write_bytes(png())
        (legacy / 'kept.png').write_bytes(png())
        (legacy / '.gitignore').write_text('*\n')
        store.save_state({'messages': [{'role': 'system', 'content': 'private-context'}]})
        original = (store.folder / 'state.json').read_bytes()
        uploaded = store.add_upload(png(), 'new.png')
        self.assertFalse(legacy.exists())
        self.assertTrue(uploaded['path'].startswith('/workspace/.recoding/inputs/'))
        self.assertEqual((inputs / 'old.png').read_bytes(), png())
        self.assertEqual((inputs / 'kept.png').read_bytes(), png())
        self.assertEqual((store.folder / 'state.json').read_bytes(), original)
        media = AgentMedia(store, hidden_paths=[store.folder.parent])
        self.assertEqual(media.bytes(uploaded['path'])[0], png())
        self.assertEqual(media.bytes('/workspace/.agent-inputs/old.png')[0], png())
        self.assertEqual(media.bytes('/workspace/.recoding.../inputs/old.png')[0], png())
        private_image = '/workspace/.recoding/' + store.folder.name + '/images/' + uploaded['sha256'] + '.png'
        with self.assertRaises(PermissionError):
            media.bytes(private_image)
        secret = store.workspace / '.recoding' / 'runtime' / 'model.env'
        secret.parent.mkdir()
        secret.write_text('secret-model-configuration')
        nested_private = secret.parent / 'private-agent'
        nested_private.mkdir()
        (nested_private / 'state.json').write_text('private-agent-history')
        with patch.dict(os.environ, {'PYTHONUSERBASE': str(self.root / 'empty-user-base')}):
            sandbox = tool_sandbox(store.workspace, network=False, gpu=False, timeout=10, hidden_files=[secret], hidden_paths=[nested_private])
        try:
            code = f"""import os
from pathlib import Path
assert sorted(p.name for p in Path('/workspace/.recoding').iterdir()) == ['inputs', 'runtime']
assert Path({uploaded['path']!r}).read_bytes() == Path('/workspace/.recoding/inputs/old.png').read_bytes()
assert not Path('/workspace/.recoding/{store.folder.name}/state.json').exists()
try:
    assert Path('/workspace/.recoding/runtime/model.env').read_text() == ''
except PermissionError:
    pass
assert not Path('/workspace/.recoding/runtime/private-agent/state.json').exists()
assert str(Path.home()) == '/opt/recoding/workspace-runtime/home'
Path.home().joinpath('.tool-state').mkdir()
Path('/workspace/.recoding/runtime/project-environment').mkdir()
Path(os.environ['XDG_CONFIG_HOME']).mkdir(parents=True, exist_ok=True)
try:
    Path('/workspace/.recoding/unexpected').mkdir()
except OSError:
    pass
else:
    raise AssertionError('private container must stay read-only')
print('inputs-visible; metadata-hidden; home-contained')
"""
            import shlex
            result = sandbox.execute('bash', json.dumps({'command': '/usr/bin/python3 -c ' + shlex.quote(code)}))
            self.assertEqual(result.get('exit_code'), 0, result)
            self.assertEqual(result['stdout'], 'inputs-visible; metadata-hidden; home-contained\n')
            seen = sandbox.execute('view_image', json.dumps({'path':'/workspace/.agent-inputs/old.png'}))
            self.assertEqual(seen['image']['path'], '/workspace/.recoding/inputs/old.png')
            renamed = sandbox.execute('view_image', json.dumps({'path':'/workspace/.recoding.../inputs/old.png'}))
            self.assertEqual(renamed['image']['path'], '/workspace/.recoding/inputs/old.png')
            shell = sandbox.execute('bash', json.dumps({'command': 'mkdir -p "$HOME/.bash-tool"; printf %s "$HOME"'}))
            self.assertEqual(shell.get('exit_code'), 0, shell)
            self.assertEqual(shell['stdout'], '/opt/recoding/workspace-runtime/home')
        finally:
            sandbox.close()
        self.assertEqual(sorted(p.name for p in store.workspace.iterdir() if p.name.startswith('.')), ['.recoding'])
        self.assertTrue((store.workspace / '.recoding' / 'runtime' / 'home' / '.tool-state').is_dir())
        self.assertTrue((store.workspace / '.recoding' / 'runtime' / 'home' / '.bash-tool').is_dir())

    def test_archive_lock_prevents_two_writers_and_releases_cleanly(self):
        store = self.create()
        first = StoreLease(store.folder)
        try:
            with self.assertRaisesRegex(ValueError, "另一个"):
                StoreLease(store.folder)
        finally:
            first.close()
        second = StoreLease(store.folder)
        second.close()

    def test_new_archive_has_portable_git_ignore_rules(self):
        store = self.create()
        self.assertEqual((store.folder / ".gitignore").read_text(), "*\n")
        self.assertEqual((store.folder.parent / ".gitignore").read_text(), "*\n")

    def test_event_pagination_recovers_all_history_and_ignores_partial_tail(self):
        store = self.create()
        for number in range(1, 910):
            store.append_event({"sequence": number, "type": "fixture", "content": str(number)})
        with (store.folder / "events.jsonl").open("a") as file:
            file.write('{"sequence":')
        recent = store.load_events()
        self.assertEqual((recent[0]["sequence"], recent[-1]["sequence"]), (110, 909))
        older = store.load_events(limit=400, before=110)
        self.assertEqual((older[0]["sequence"], older[-1]["sequence"]), (1, 109))


class RestoreTests(unittest.TestCase):
    def test_restore_closes_uncommitted_tools_preserves_images_and_never_replays_code(self):
        client, executor = Mock(), Mock()
        agent = Agent(client, executor, system="initial")
        picture, _ = make_tool_message([], "image", image_result())
        messages = [{"role": "system", "content": "saved"},
                    {"role": "user", "content": "原任务"},
                    {"role": "assistant", "tool_calls": [call("image"), call("pending", "python", code="dangerous()")]}, picture]
        original = deepcopy(messages)
        agent.restore(messages, iteration=7, tool_count=1, runtime_note={"note": "fresh runtime"})
        executor.execute.assert_not_called()
        client.complete.assert_not_called()
        self.assertEqual(messages, original)
        self.assertEqual(agent.model_step, 7)
        self.assertEqual(json.loads(agent.messages[-1]["content"])["status"], "interrupted")
        wire = to_api_messages(agent.messages)
        self.assertEqual(sum(isinstance(item.get("content"), list) for item in wire), 1)
        client.last_usage = {}
        client.complete.return_value = {"role": "assistant", "content": "继续新的任务。"}
        drive(agent, "继续")
        self.assertEqual(agent.model_step, 8)
        executor.execute.assert_not_called()

    def test_invalid_restore_leaves_current_context_unchanged(self):
        agent = Agent(Mock(), Mock(), system="current")
        for messages in ([], [None], [{"role": "user", "content": "bad"}],
                         [{"role": "system", "content": "saved"}, {"role": "tool", "tool_call_id": "missing", "content": "bad"}]):
            with self.assertRaises(ValueError):
                agent.restore(messages)
            self.assertEqual(agent.messages, [{"role": "system", "content": "current"}])
