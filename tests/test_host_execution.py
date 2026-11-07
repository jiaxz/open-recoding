"""Explicit Host execution and unchanged isolation defaults, using offline fixtures."""
from pathlib import Path
import json
import os
import shlex
import time
import unittest
import zipfile
from unittest.mock import patch

from multiagent.config import configuration
from multiagent.media import AgentMedia
from multiagent.storage import StoreLease
from session import Session, SessionStore, SavedSession
from node.environment import ExecutionEnvironment
from tools import execution_sources
from tools.registry import ToolRegistry
from node.packages import package_directory
from workspace_support import temporary_workspace


class HostExecutionTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(temporary_workspace(prefix='host-execution-').name)
        self.env=patch.dict(os.environ,{'PYTHONUSERBASE':str(self.root/'empty-user-site'),
                                     'RECODING_API_KEY':'must-not-reach-tools'})
        self.env.start();self.addCleanup(self.env.stop)
        self.session=Session.create(self.root/'project with spaces','Host fixture',
            config={'demo':True,'execution_mode':'host','tool_timeout':5})
        self.addCleanup(self.session.close)
        self.wait(lambda:self.session.master.status=='sleeping')

    def wait(self,predicate,seconds=10):
        end=time.monotonic()+seconds
        while not predicate():
            self.assertNotEqual(self.session.master.status,'failed',self.session.master.error)
            self.assertLess(time.monotonic(),end,'Host fixture timed out')
            time.sleep(.02)

    def call(self,tool_name,**fields):
        return self.session.team.dispatch('master',tool_name,fields)

    def test_native_processes_persist_async_notify_and_cleanup(self):
        backend=self.session.execution.environment.sandbox
        self.assertEqual(backend.mode,'host')
        prompt=self.session.store.load_state()['messages'][0]['content']
        self.assertIn('Execution mode: HOST',prompt)
        self.assertNotIn('Execution mode: SANDBOX',prompt)
        self.assertIn(str(self.session.directory),prompt)
        self.assertEqual(self.session.snapshot(events=False)['execution_mode'],'host')
        outside=self.root/'outside-project.txt';outside.write_text('HOST_ACCESS')
        bash=self.call('bash',command='pwd; cat '+shlex.quote(str(outside)))
        self.assertEqual(bash['exit_code'],0,bash)
        self.assertEqual(bash['stdout'].splitlines(),[str(self.session.directory),'HOST_ACCESS'])
        shell=self.call('bash_process',action='create',name='Host shell')['process']['id']
        self.assertEqual(self.call('bash',process_id=shell,command='counter=42; greet(){ printf hello; }')['exit_code'],0)
        self.assertEqual(self.call('bash',process_id=shell,command='printf %s "$counter"; greet')['stdout'],'42hello')
        py=self.call('python_process',action='create',name='Host Python')['process']['id']
        self.assertEqual(self.call('python',process_id=py,code='counter=42')['exit_code'],0)
        pending=self.call('python',process_id=py,mode='async',code="import time,os; time.sleep(.1); print(counter); print('RECODING_API_KEY' in os.environ)")
        identifier=pending['execution']['id']
        self.wait(lambda:self.call('python_execution',action='status',execution_id=identifier)['execution']['status']=='succeeded')
        self.assertEqual(self.call('python_execution',action='read',execution_id=identifier)['stdout'],'42\nFalse\n')
        self.wait(lambda:any(m.get('configuration',{}).get('execution_id')==identifier for m in self.session.team.data['messages']))
        supervisor=backend.session.process
        self.session.close();self.assertIsNotNone(supervisor.poll())
        self.assertTrue(all(p['status']=='closed' for p in self.session.python_pool.snapshot()['processes'].values()))
        self.assertEqual(outside.read_text(),'HOST_ACCESS')

    def test_real_paths_edit_navigation_images_and_workspace_packages(self):
        path=self.session.directory/'hello.py';path.write_text('value = 1\n')
        read=self.call('read_file',path=str(path))['data']
        self.assertEqual(read['path'],str(path))
        edited=self.call('edit_files',changes=[{'path':str(path),'expected_sha256':read['sha256'],'content':'value = 2\n'}])
        self.assertEqual(edited['exit_code'],0,edited);self.assertEqual(path.read_text(),'value = 2\n')
        query=self.call('repo_query',path=str(self.session.directory),mode='files')
        self.assertEqual(query['exit_code'],0,query)
        self.assertIn(str(path),json.dumps(query['data']))
        picture=self.session.directory/'figure.jpg'
        picture.write_bytes((Path(__file__).parent/'fixtures/small.jpg').read_bytes())
        viewed=self.call('view_image',path=str(picture))
        self.assertEqual(viewed['image']['path'],str(picture))
        self.assertEqual(AgentMedia(self.session.store).read(str(picture))['image']['sha256'],viewed['image']['sha256'])
        self.assertEqual(self.call('view_image',path='/workspace/figure.jpg')['image']['sha256'],viewed['image']['sha256'])
        package_directory(self.session.directory).joinpath('host_fixture_aux.py').write_text('VALUE=73\n')
        result=self.call('python',code='import host_fixture_aux; print(host_fixture_aux.VALUE)')
        self.assertEqual(result['stdout'],'73\n',result)
        inspected=self.call('inspect_python_environment')
        self.assertEqual(inspected['exit_code'],0,inspected)
        self.assertEqual(inspected['data']['package_roots'][0]['path'],str(package_directory(self.session.directory)))
        wheel=self.session.directory/'host_fixture_wheel-1.0-py3-none-any.whl'
        with zipfile.ZipFile(wheel,'w') as archive:
            archive.writestr('host_fixture_wheel.py','VALUE=91\n')
            dist='host_fixture_wheel-1.0.dist-info/'
            archive.writestr(dist+'METADATA','Metadata-Version: 2.1\nName: host-fixture-wheel\nVersion: 1.0\n')
            archive.writestr(dist+'WHEEL','Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
            archive.writestr(dist+'RECORD','')
        installed=self.call('install_packages',packages=[str(wheel)],no_index=True,no_deps=True)
        self.assertEqual(installed['exit_code'],0,installed)
        self.assertEqual(self.call('python',code='import host_fixture_wheel; print(host_fixture_wheel.VALUE)')['stdout'],'91\n')

    def test_mode_is_saved_and_validated_before_starting_or_leaking_locks(self):
        folder=self.session.store.folder
        self.session.close()
        self.assertEqual(SessionStore(folder).settings()['execution_mode'],'host')
        self.assertEqual(SavedSession(folder).snapshot(events=False)['execution_mode'],'host')
        with self.assertRaisesRegex(ValueError,'cannot enforce'):
            Session.resume(folder,config={'demo':True,'network':False})
        with StoreLease(folder):pass
        resumed=Session.resume(folder,config={'demo':True,'execution_mode':'sandbox','network':False,'gpu':False})
        self.addCleanup(resumed.close)
        self.session=resumed;self.wait(lambda:resumed.master.status=='sleeping')
        self.assertEqual(resumed.execution.environment.sandbox.mode,'sandbox')
        self.assertEqual(SessionStore(folder).settings()['execution_mode'],'sandbox')


class ExecutionModeConfigurationTests(unittest.TestCase):
    def test_explicit_modes_reject_invalid_constraints_and_never_fallback(self):
        self.assertEqual(configuration()['execution_mode'],'sandbox')
        for values in ({'execution_mode':'invalid'},{'execution_mode':'host','network':False},{'execution_mode':'host','gpu':False}):
            with self.subTest(values=values),self.assertRaises(ValueError):configuration(values)
        from recoding_cli.main import parser,options
        self.assertEqual(options(parser().parse_args(['chat','--execution-mode','host']))['execution_mode'],'host')
        with temporary_workspace() as directory:
            store=SessionStore.create(Path(directory)/'workspace','Default sandbox',{})
            env=ExecutionEnvironment(store,{'network':False,'gpu':False},sources=execution_sources(),registry_factory=ToolRegistry)
            with patch('node.environment.Sandbox',side_effect=RuntimeError('sandbox blocked')),patch('node.environment.HostExecution') as host:
                with self.assertRaisesRegex(RuntimeError,'sandbox blocked'):env.ensure()
                host.assert_not_called()
            env.close()
            from sandbox_support import HostExecution
            with patch('node.backends.sandbox.shutil.which',return_value=None):
                native=HostExecution(store.workspace)
            try:
                self.assertIsNone(native.bwrap)
                native.check()
                result=native.session.request('command',argv=['/bin/pwd'])
                self.assertEqual(result['stdout'].strip(),str(store.workspace))
            finally:native.close()


class HostAppTests(unittest.TestCase):
    def test_cli_startup_flag_and_finite_driver(self):
        import subprocess,sys
        with temporary_workspace() as directory:
            workspace=Path(directory)/'workspace'
            reply=subprocess.run([sys.executable,'-B','recoding-cli.py','run','--demo','--execution-mode','host',
                '-C',str(workspace),'-p','Check the current workspace','--max-messages','1','--timeout','10',
                '--format','jsonl','--no-color'],cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=15)
            self.assertEqual(reply.returncode,0,reply.stderr+reply.stdout)
            folder=next((workspace/'.recoding').glob('*/session.json')).parent
            self.assertEqual(SessionStore(folder).settings()['execution_mode'],'host')
            self.assertIn(str(workspace),reply.stdout)
            from session import SessionHostProtocol
            self.assertTrue(SessionHostProtocol.available(folder))

    def test_web_create_browse_and_mode_change_only_on_resume(self):
        import threading,uuid
        from recoding_web.host import GuiSessionHost
        from recoding_web.server import GuiHttpServer
        from test_web import WebTests
        with temporary_workspace() as directory:
            self.root=Path(directory);self.page=uuid.uuid4().hex
            self.app=GuiSessionHost(self.root,demo=True)
            self.server=GuiHttpServer(('127.0.0.1',0),self.app)
            thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.02},daemon=True);thread.start()
            self.request=lambda *args,**kw:WebTests.request(self,*args,**kw)
            try:
                status,created=self.request('/api/sessions',{'action':'create','execution_mode':'host'})
                self.assertEqual(status,201,created)
                identifier=created['id'];session=self.app.running(identifier)
                WebTests.wait(self,identifier,lambda s:s['master_status']=='sleeping')
                self.assertEqual(session.config['execution_mode'],'host')
                status,error=self.request(f'/api/sessions/{identifier}/continue',{'settings':{'execution_mode':'sandbox'}})
                self.assertEqual(status,400,error)
                self.assertEqual(self.request(f'/api/sessions/{identifier}/close',{})[0],200)
                original=(session.store.folder/'session.json').read_bytes()
                status,browsed=self.request('/api/sessions',{'action':'browse','id':identifier})
                self.assertEqual(status,201,browsed);self.assertTrue(browsed['read_only'])
                self.assertEqual(browsed['execution_mode'],'host')
                self.assertEqual((session.store.folder/'session.json').read_bytes(),original)
                self.app.config['network']=False
                status,error=self.request(f'/api/sessions/{identifier}/continue',{})
                self.assertEqual(status,400,error)
                self.assertEqual((session.store.folder/'session.json').read_bytes(),original)
                self.app.config['network']=True
                status,result=self.request(f'/api/sessions/{identifier}/continue',{'settings':{'execution_mode':'sandbox'}})
                self.assertEqual(status,202,result)
                WebTests.wait(self,identifier,lambda s:s['master_status']=='sleeping')
                self.assertEqual(SessionStore(session.store.folder).settings()['execution_mode'],'sandbox')
            finally:
                try:self.app.close()
                finally:
                    for session in list(self.app.sessions.values()):session.close()
                    self.server.shutdown();self.server.server_close();thread.join(3)
