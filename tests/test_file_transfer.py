"""Offline file channels and builtin isolation; all work stays in sessions/tests."""
from contextlib import ExitStack
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from node.files import CHUNK, WorkspaceFiles
from node.file_relay import copy_files
from tools import definitions
from session import Session
from workspace_support import temporary_workspace


class FileTransferTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(temporary_workspace(prefix='file-transfer-').name)

    def wait(self, predicate):
        until = time.monotonic() + 12
        while not predicate():
            self.assertLess(time.monotonic(), until, 'Operation did not finish')
            time.sleep(.02)

    def test_conflicts_source_mutation_and_private_paths_do_not_publish_partial_file(self):
        src = self.root / 'source'; dst = self.root / 'target'; src.mkdir(); dst.mkdir()
        (src / 'input.bin').write_bytes(b'a' * (CHUNK + 17)); (dst / 'output.bin').write_bytes(b'original')
        def run(**options):
            with ExitStack() as stack:
                a = WorkspaceFiles(src); stack.callback(a.close)
                b = WorkspaceFiles(dst); stack.callback(b.close)
                return copy_files(a, b, 'input.bin', 'output.bin', **options)
        with self.assertRaises(FileExistsError): run()
        self.assertEqual((dst / 'output.bin').read_bytes(), b'original')
        changed = False
        def mutate(**values):
            nonlocal changed
            if values.get('bytes_transferred') and not changed:
                changed = True; (src / 'input.bin').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'Source changed'): run(overwrite=True, progress=mutate)
        self.assertEqual((dst / 'output.bin').read_bytes(), b'original')
        self.assertFalse(list(dst.glob('recoding-transfer-*.part')))
        stopped = False
        def interrupt(**values):
            nonlocal stopped
            if values.get('bytes_transferred'): stopped = True
        (src / 'input.bin').write_bytes(b'b' * (CHUNK + 17))
        with self.assertRaises(InterruptedError): run(overwrite=True, progress=interrupt, cancelled=lambda: stopped)
        self.assertEqual((dst / 'output.bin').read_bytes(), b'original')
        self.assertFalse(list(dst.glob('recoding-transfer-*.part')))
        (src / '.recoding').mkdir(); (src / '.recoding' / 'secret').write_text('archive')
        (src / '.env').write_text('private')
        with ExitStack() as stack:
            a = WorkspaceFiles(src); stack.callback(a.close)
            for path in ('../outside', '.env', '.recoding/secret'):
                with self.assertRaises(ValueError): a.request('manifest', path=path)
            (src / 'link').symlink_to(src / 'input.bin')
            with self.assertRaisesRegex(ValueError, 'Symlinks'): a.request('manifest', path='link')

    def test_non_builtin_session_tool_async_notifications_permissions_and_cancellation(self):
        session = Session.create(self.root / 'workspace', 'File tool fixture', config={'demo': True, 'network': False, 'gpu': False,'remote_nodes':{'fixture':{'ssh_target':'fixture-alias','workspace':'/path/to/project'}}})
        self.addCleanup(session.close)
        self.wait(lambda: session.master.status == 'sleeping')
        (session.directory / 'input.txt').write_text('transfer fixture')
        notices = []; session.execution.file_transfers.notify = lambda *args: notices.append(args)
        transfer = session.team.dispatch('master', 'transfer_files', {'source_path': 'input.txt', 'destination_path': 'output.txt', 'mode': 'async'})['transfer']
        self.wait(lambda: session.team.dispatch('master', 'transfer_files', {'action': 'status', 'transfer_id': transfer['id']})['transfer']['status'] == 'succeeded')
        self.wait(lambda: len(notices) == 1)
        self.assertEqual((session.directory / 'output.txt').read_text(), 'transfer fixture')
        targets = session.team.dispatch('master', 'transfer_files', {'action': 'nodes'})
        self.assertNotIn('ssh_target', json.dumps(targets)); self.assertEqual(targets['nodes'][1]['capabilities'], ['files'])
        with self.assertRaisesRegex(ValueError, 'network'):
            session.team.dispatch('master', 'transfer_files', {'source_path': 'input.txt', 'destination_node': 'fixture', 'destination_path': 'x'})
        cancelled = session.execution.file_transfers.request('master', {'source_path': 'input.txt', 'destination_path': 'cancelled.txt'}, cancelled=lambda: True)
        self.assertEqual(cancelled['transfer']['status'], 'cancelled'); self.assertFalse((session.directory / 'cancelled.txt').exists())
        self.assertIn('transfer_files', {row['schema']['function']['name'] for row in definitions()})

    def test_builtin_management_messages_sleep_and_recall_do_not_call_node(self):
        from multiagent.tools import HostTools, CONTEXT_TOOLS
        from multiagent.tools import AGENT_TOOLS
        builtin = {row['function']['name'] for row in [*CONTEXT_TOOLS, *AGENT_TOOLS]}
        self.assertFalse(builtin & {row['schema']['function']['name'] for row in definitions()})
        session = Session.create(self.root / 'workspace', 'Builtin fixture', config={'demo': True, 'network': False, 'gpu': False})
        self.addCleanup(session.close)
        self.wait(lambda: session.master.status == 'sleeping')
        with patch.object(session.execution, 'request', side_effect=AssertionError('Builtin queried a Node')):
            call = lambda tool_name, **fields: session.team.dispatch('master', tool_name, fields)
            key = call('create_agent', name='Host-only peer', permissions={'tools': []})['agent_id']
            self.wait(lambda: session.team._agent(key)['status'] == 'sleeping')
            call('rename_agent', agent_id=key, name='Renamed peer')
            call('set_agent_icon', agent_id=key, icon='📎')
            call('list_agents'); call('sleep', seconds=0)
            call('send_agent_message', agent_id=key, message='Host mail')
            call('broadcast_agent_message', message='Host broadcast')
            call('send_user_message', message='Host report')
            call('read_global_messages'); call('listen_global_messages', seconds=0)
            call('stop_agent', agent_id=key, reason='Offline builtin isolation check')
            self.wait(lambda: session.team._agent(key)['status'] == 'stopped')
            call('resume_agent', agent_id=key)
            self.wait(lambda: session.team._agent(key)['status'] == 'sleeping')
            class RejectNode:
                def execute(self, *_): raise AssertionError('Recall delegated to a Node')
            result = HostTools(RejectNode(), lambda: [{'role': 'system', 'content': 'fixture'}, {'role': 'user', 'content': 'remember me'}]).execute('recall_context', '{}')
            self.assertIn('remember me', json.dumps(result))
