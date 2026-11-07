"""Common opt-in Host protocol, kernel lifecycle, and the two application hosts."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from multiagent.agent import Agent
from multiagent.storage import StoreLease
from session import Session,SessionStore,SavedSession,SessionHostProtocol
from recoding_cli.client import Client,NoSessionClient
from recoding_cli.host import CliSessionHost
from recoding_cli.sessions import CliSessionNavigator
from recoding_web.host import GuiSessionHost,GuiBusyError
from workspace_support import temporary_workspace

CONFIG={'demo':True,'network':False,'gpu':False}

def fake_start(agent):
    agent.status='running'
    return {'status':'starting'}


class SessionOwnershipTests(unittest.TestCase):
    def test_shared_readonly_browsing_is_revoked_when_any_host_resumes(self):
        with temporary_workspace(prefix='host-browsing-') as root,patch.object(Agent,'start_worker',fake_start):
            store=SessionStore.create(root,'shared',{})
            a,b=[GuiSessionHost(root,CONFIG) for _ in range(2)]
            self.addCleanup(a.close);self.addCleanup(b.close)
            original=(store.folder/'session.json').read_bytes()
            views=[host.open(identifier=store.id,action='browse') for host in (a,b)]
            self.assertTrue(all(isinstance(view,SavedSession) for view in views))
            self.assertEqual(a.sessions,{})
            self.assertEqual(b.sessions,{})
            with self.assertRaises(PermissionError):a.set_agent_limit(store.id,{'max_subagents':9})
            self.assertEqual((store.folder/'session.json').read_bytes(),original)
            self.assertEqual(a.continue_task(store.id,{'max_subagents':3})['status'],'run')
            self.assertEqual(SessionStore(store.folder).settings()['max_subagents'],3)
            for action in [lambda:b.open(identifier=store.id,action='browse'),lambda:views[1].snapshot(),
                           lambda:views[1].messages(),lambda:b.continue_task(store.id,{'max_subagents':99}),
                           lambda:CliSessionHost(store.folder,CONFIG)]:
                with self.assertRaises((ValueError,PermissionError)):action()
            # A stopped/failed Agent does not release the process's hosted Session.
            a.sessions[store.id].master.status='failed'
            self.assertFalse(SessionHostProtocol.available(store.folder))
            self.assertTrue(b.catalog.listing()[0]['busy'])
            a.close_session(store.id)
            self.assertEqual(views[1].snapshot()['status'],'stop')
            self.assertTrue(SessionHostProtocol.available(store.folder))

    def test_direct_python_interfaces_are_outside_host_protocol(self):
        with temporary_workspace(prefix='host-sdk-') as root:
            store=SessionStore.create(root,'sdk',{})
            host_lock=StoreLease(store.folder,name='host.lock')
            self.addCleanup(host_lock.close)
            self.assertEqual(SavedSession(store.folder).snapshot()['status'],'stop')
            with self.assertRaises(ValueError):SessionHostProtocol.browse(store.folder)
            # Direct Session may deliberately run without host.lock.
            direct=Session(store,CONFIG,autostart=False)
            try:self.assertIsNone(direct.host_lease)
            finally:direct.close()
            host_lock.close()
            with patch.object(Agent,'start_worker',fake_start):
                direct=Session.resume(store.folder,config=CONFIG)
                try:self.assertTrue(SessionHostProtocol.available(store.folder))
                finally:direct.close()

    def test_resume_race_one_winner_and_failed_start_does_not_save_settings(self):
        with temporary_workspace(prefix='host-race-') as root:
            store=SessionStore.create(root,'race',{})
            hosts=[GuiSessionHost(root,CONFIG) for _ in range(2)]
            for host in hosts:self.addCleanup(host.close)
            def resume(index):
                try:hosts[index].continue_task(store.id,{'max_subagents':index+4});return index
                except ValueError:return None
            with patch.object(Agent,'start_worker',fake_start),ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(resume,range(2)))
            winners=[i for i in results if i is not None];self.assertEqual(len(winners),1)
            self.assertEqual(SessionStore(store.folder).settings()['max_subagents'],winners[0]+4)
            hosts[winners[0]].close_session(store.id)
            original=(store.folder/'session.json').read_bytes()
            with patch.object(Agent,'start_worker',side_effect=RuntimeError('launch failed')):
                with self.assertRaises(RuntimeError):hosts[0].continue_task(store.id,{'max_subagents':9})
            self.assertEqual((store.folder/'session.json').read_bytes(),original)
            self.assertTrue(SessionHostProtocol.available(store.folder))

    def test_gui_single_page_gate_is_separate_from_multi_session_ownership(self):
        with temporary_workspace(prefix='host-gui-') as root,patch.object(Agent,'start_worker',fake_start):
            host=GuiSessionHost(root,CONFIG,gui_timeout=30);self.addCleanup(host.close)
            a=host.open('a',action='create');b=host.open('b',action='create')
            with patch('recoding_web.host.time.monotonic',return_value=100):
                self.assertIsNone(host.connect_gui('page-a'))
                self.assertIsNone(host.connect_gui('page-a'))
            with patch('recoding_web.host.time.monotonic',return_value=110):
                with self.assertRaises(GuiBusyError):host.connect_gui('page-b')
            with patch('recoding_web.host.time.monotonic',return_value=131):
                self.assertEqual(host.connect_gui('page-b'),'page-a')
            self.assertEqual(len(host.sessions),2)
            self.assertFalse(SessionHostProtocol.available(a.store.folder))
            self.assertFalse(SessionHostProtocol.available(b.store.folder))
            host.close_session(a.id)
            self.assertTrue(SessionHostProtocol.available(a.store.folder))
            self.assertFalse(SessionHostProtocol.available(b.store.folder))

    def test_cli_switch_stops_old_session_and_never_browses_offline(self):
        with temporary_workspace(prefix='host-cli-') as root,patch.object(Agent,'start_worker',fake_start):
            a,b=[SessionStore.create(root,name,{}) for name in ('one','two')]
            owner=CliSessionHost(a.folder,CONFIG)
            nav=CliSessionNavigator(root,lambda _:CONFIG,initial=owner,initial_client=Client(a.folder))
            self.addCleanup(nav.close)
            with self.assertRaises(ValueError):Client(b.folder).call('status')
            next_client=nav.restore(b.id)
            self.assertTrue(SessionHostProtocol.available(a.folder))
            self.assertFalse(SessionHostProtocol.available(b.folder))
            self.assertEqual(next_client.call('status')['status'],'run')
            next_client.call('stop');nav.stop()
            self.assertTrue(SessionHostProtocol.available(b.folder))
            self.assertIsInstance(nav.current_client,NoSessionClient)
            with self.assertRaises(ValueError):next_client.call('messages')
            restored=nav.restore(a.id)
            self.assertEqual(restored.call('status')['status'],'run')
            nav.close()
            self.assertTrue(SessionHostProtocol.available(a.folder))

    def test_resume_picker_only_reads_metadata_in_current_directory(self):
        with temporary_workspace(prefix='host-picker-') as root:
            here=SessionStore.create(root,'here',{})
            nested=SessionStore.create(Path(root)/'child'/'nested','there',{})
            nav=CliSessionNavigator(root,lambda _:self.fail('Browsing must not start a Host'))
            self.addCleanup(nav.close)
            self.assertEqual([s['id'] for s in nav.listing()['sessions']],[here.id])
            self.assertEqual(nav.change_directory('child')['sessions'],[])
            self.assertEqual([s['id'] for s in nav.change_directory('nested')['sessions']],[nested.id])
            self.assertEqual(nav.change_directory('..')['directory'],str(nested.workspace.parent))

    def test_old_cli_cleanup_and_client_cannot_touch_a_successor_host(self):
        with temporary_workspace(prefix='host-generation-') as root,patch.object(Agent,'start_worker',fake_start):
            store=SessionStore.create(root,'handoff',{})
            old=CliSessionHost(store.folder,CONFIG);self.addCleanup(old.close)
            previous=Client(store.folder)
            self.assertEqual(previous.call('status')['status'],'run')
            previous.call('stop')
            successor=CliSessionHost(store.folder,CONFIG);self.addCleanup(successor.close)
            old.close()
            self.assertEqual(Client(store.folder).call('status')['status'],'run')
            with self.assertRaises(ValueError):previous.call('stop')
            self.assertEqual(Client(store.folder).call('status')['status'],'run')

    def test_process_death_releases_host_lock_without_child_inheritance(self):
        with temporary_workspace(prefix='host-kill-') as root:
            store=SessionStore.create(root,'killed',{})
            program='''import json,subprocess,sys,time
from multiagent.storage import StoreLease
lease=StoreLease(sys.argv[1],name='host.lock')
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],close_fds=True)
print(json.dumps({'child':child.pid,'inheritable':__import__('os').get_inheritable(lease.fd)}),flush=True)
time.sleep(60)
'''
            parent=subprocess.Popen([sys.executable,'-u','-c',program,str(store.folder)],stdout=subprocess.PIPE,text=True)
            child=None
            try:
                info=json.loads(parent.stdout.readline());child=info['child']
                self.assertFalse(info['inheritable'])
                self.assertFalse(SessionHostProtocol.available(store.folder))
                parent.kill();parent.wait(timeout=5)
                self.assertTrue(SessionHostProtocol.available(store.folder))
                lease=StoreLease(store.folder,name='host.lock');lease.close()
                self.assertTrue((store.folder/'host.lock').exists(),'The lock file is never removed to unlock')
            finally:
                if parent.poll() is None:parent.kill();parent.wait(timeout=5)
                parent.stdout.close()
                if child:
                    try:os.kill(child,15)
                    except ProcessLookupError:pass

    def test_actual_host_death_reaps_worker_and_allows_reopen(self):
        with temporary_workspace(prefix='host-worker-death-') as root:
            store=SessionStore.create(root,'worker-death',{})
            program='''import json,sys,time
from recoding_web.host import GuiSessionHost
host=GuiSessionHost(sys.argv[1],demo=True,network=False,gpu=False)
session=host.open(identifier=sys.argv[2],action='resume')
print(json.dumps({'worker':session.master.process.pid}),flush=True)
time.sleep(60)
'''
            parent=subprocess.Popen([sys.executable,'-u','-c',program,str(root),store.id],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            worker=None
            try:
                line=parent.stdout.readline()
                self.assertTrue(line,parent.stderr.read() if not line else '')
                info=json.loads(line);worker=info['worker']
                self.assertFalse(SessionHostProtocol.available(store.folder))
                parent.kill();parent.wait(timeout=5)
                self.assertTrue(SessionHostProtocol.available(store.folder))
                deadline=time.monotonic()+10
                while not StoreLease.available(store.folder):
                    self.assertLess(time.monotonic(),deadline,'Worker retained its resource lease after Host death')
                    time.sleep(.05)
                successor=SessionHostProtocol.resume(store.folder,config=CONFIG)
                successor.close()
            finally:
                if parent.poll() is None:parent.kill();parent.wait(timeout=5)
                parent.stdout.close();parent.stderr.close()
                if worker and Path(f'/proc/{worker}').exists():
                    try:os.kill(worker,15)
                    except ProcessLookupError:pass


if __name__=='__main__':unittest.main()
