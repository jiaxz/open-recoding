"""Real loopback OpenSSH nodes; no model API, user SSH config or existing Session."""
from contextlib import ExitStack
import hashlib,json,os,base64
from pathlib import Path
import time
import unittest

from session import Session,SavedSession
from unittest.mock import patch
from ssh_support import local_ssh
from workspace_support import temporary_workspace


class NodeRuntimeTests(unittest.TestCase):
    def wait(self,predicate,seconds=15):
        deadline=time.monotonic()+seconds
        while not predicate():
            self.assertLess(time.monotonic(),deadline,'Node operation exceeded deadline');time.sleep(.03)

    def test_real_ssh_node_pools_reconnect_transfer_files_and_cleanup(self):
        root=Path(temporary_workspace(prefix='node-runtime-ssh-').name)
        report={'transport':'real OpenSSH over loopback','cases':[]}
        with ExitStack() as stack:
            a=stack.enter_context(local_ssh(root/'ssh-a'));b=stack.enter_context(local_ssh(root/'ssh-b'))
            session=Session.create(root/'workspace','Node runtime fixture',config={'demo':True,'network':True,'gpu':False,'tool_timeout':5})
            stack.callback(session.close)
            self.wait(lambda:session.master.status=='sleeping')
            for key,fixture in [('a',a),('b',b)]:
                session.configure_remote_node(key,name='Compute '+key,ssh_target=fixture.alias,workspace=str(fixture.remote),ssh_config=str(fixture.ssh_config))
            call=lambda tool_name,**fields:session.team.dispatch('master',tool_name,fields)
            pid_a=call('python_process',action='create',node_id='a',name='Shared Python')['process']['id']
            pid_b=call('python_process',action='create',node_id='b',name='Parallel Python')['process']['id']
            with patch.object(session.execution.resources['a'].store,'workspace',Path('/not-mounted-on-the-control-host')):
                call('python',process_id=pid_a,code='value=41')
                self.assertEqual(call('python',process_id=pid_a,code='print(value)')['stdout'],'41\n')
            self.assertEqual(call('python',process_id=pid_a,code='print(value+1)')['stdout'],'42\n')
            with self.assertRaisesRegex(ValueError,'different node'):call('python',process_id=pid_a,node_id='b',code='print(0)')
            first=call('python',process_id=pid_a,code='import time;time.sleep(.3);print("a")',mode='async')['execution']['id']
            second=call('python',process_id=pid_b,code='import time;time.sleep(.3);print("b")',mode='async')['execution']['id']
            self.wait(lambda:call('python_execution',action='status',execution_id=first)['execution']['status']=='succeeded')
            self.wait(lambda:call('python_execution',action='status',execution_id=second)['execution']['status']=='succeeded')
            self.assertEqual(call('python_execution',action='read',execution_id=second)['stdout'],'b\n')
            timings=[call('python_execution',action='status',execution_id=key)['execution'] for key in (first,second)]
            self.assertLess(max(row['started_at'] for row in timings),min(row['finished_at'] for row in timings))
            report['cases'].append('Remote Python state, concurrent async execution and ID routing')
            call('bash',node_id='b',command="printf 'remote text' > note.txt")
            self.assertEqual(call('read_file',node_id='b',path='note.txt')['data']['content'],'remote text')
            from test_images import png
            image=png();encoded=base64.b64encode(image).decode()
            call('python',process_id=pid_b,code='import base64\nfrom pathlib import Path\nPath("figure.png").write_bytes(base64.b64decode('+repr(encoded)+'))')
            viewed=call('view_image',node_id='b',path='figure.png')
            self.assertEqual(viewed['image']['sha256'],hashlib.sha256(image).hexdigest())
            self.assertEqual(viewed['node_id'],'b')
            report['cases'].append('Remote command adapter, path tool and image bytes')
            shell=call('bash_process',action='create',node_id='a')['process']['id']
            call('bash',process_id=shell,command='answer=42;cd nested')
            backend=session.execution.resources['a'].environment.sandbox;runtime_id=backend.session.runtime_id
            for _ in range(5):
                backend.session.close('Test transient SSH disconnect')
                output=call('bash',process_id=shell,command='printf "%s:" "$answer";pwd')['stdout']
                self.assertEqual(output,'42:/workspace/nested\n');self.assertEqual(backend.session.runtime_id,runtime_id)
            report['reconnect_cycles']=5
            self.assertEqual(call('python',process_id=pid_a,code='print(value)')['stdout'],'41\n')
            report['cases'].append('SSH reconnect preserves Bash/Python memory and runtime identity')
            peer=call('create_agent',name='SSH collaborator',permissions={'tools':['python','python_process','python_execution']})['agent_id']
            self.wait(lambda:session.team._agent(peer)['status']=='sleeping')
            self.assertEqual(session.team.dispatch(peer,'python',{'process_id':pid_a,'code':'print(value)'})['stdout'],'41\n')
            call('python_process',action='transfer',process_id=pid_a,new_owner=peer)
            session.team.dispatch(peer,'python_process',{'action':'transfer','process_id':pid_a,'new_owner':'master'})
            call('stop_agent',agent_id=peer,reason='Remote explicit transfer regression')
            self.wait(lambda:session.team._agent(peer)['status']=='stopped')
            self.assertEqual(call('python',process_id=pid_a,code='print(value)')['stdout'],'41\n')
            report['cases'].append('Remote open access and explicit ownership transfer before sub Stop')
            call('python',process_id=pid_a,code='from pathlib import Path\np=Path("artifacts");p.mkdir();(p/"empty").mkdir();(p/"中文.bin").write_bytes(bytes(range(256))*4097)')
            transfer=call('transfer_files',source_node='a',source_path='artifacts',destination_node='b',destination_path='copied',mode='async')['transfer']['id']
            self.wait(lambda:call('transfer_files',action='status',transfer_id=transfer)['transfer']['status'] in {'succeeded','failed'})
            state=call('transfer_files',action='status',transfer_id=transfer)['transfer'];self.assertEqual(state['status'],'succeeded',state)
            digest=hashlib.sha256(bytes(range(256))*4097).hexdigest()
            self.assertEqual(call('python',process_id=pid_b,code='import hashlib\nfrom pathlib import Path\nprint(hashlib.sha256(Path("copied/中文.bin").read_bytes()).hexdigest())')['stdout'].strip(),digest)
            self.assertEqual(call('transfer_files',source_node='b',source_path='copied/中文.bin',destination_path='returned.bin')['transfer']['status'],'succeeded')
            self.assertEqual(hashlib.sha256((session.directory/'returned.bin').read_bytes()).hexdigest(),digest)
            report['cases'].append('Remote A → Host → Remote B → Host binary/Unicode/empty-directory copy')
            resources=call('pool_status');self.assertEqual({r['node_id'] for r in resources['processes']},{'a','b'})
            brokers=[]
            for fixture in (a,b):
                brokers.extend(json.loads(path.read_text()) for path in (fixture.remote/'.recoding/runtime/nodes').rglob('runtime.json'))
            archive=session.store.folder
            session.cancel()
            self.wait(lambda:all(not Path('/proc',str(row['pid'])).exists() for row in brokers))
            saved=SavedSession(archive)
            self.assertEqual({row['id'] for row in saved.snapshot(events=False)['nodes']},{'local','a','b'})
            self.assertEqual(saved.execution_output('master',second)['stdout'],'b\n')
            report['cases'].append('Session Stop closes all runtimes; stopped browsing reads cached remote logs')
        report['passed']=True
        if os.environ.get('RECODING_NODE_REPORT'):
            path=Path(os.environ['RECODING_NODE_REPORT']);path.parent.mkdir(mode=0o700,parents=True,exist_ok=True);path.write_text(json.dumps(report,ensure_ascii=False,indent=2))

    def test_disconnected_runtime_lease_reclaims_resources_without_a_controller(self):
        root=Path(temporary_workspace(prefix='node-runtime-lease-').name)
        with local_ssh(root/'ssh') as fixture:
            session=Session.create(root/'workspace','Lease fixture',config={'demo':True,'network':True,'gpu':False})
            try:
                session.configure_remote_node('leased',ssh_target=fixture.alias,workspace=str(fixture.remote),ssh_config=str(fixture.ssh_config),lease_seconds=10)
                resource=session.execution.resources['leased'];resource.halt.set();resource.monitor.join(timeout=3)
                resource.environment.monitor.close()
                backend=resource.environment.sandbox
                record=json.loads(next((fixture.remote/'.recoding/runtime/nodes').rglob('runtime.json')).read_text())
                previous_runtime=backend.session.runtime_id
                backend.session.close('Simulated lost controller; no shutdown request')
                self.wait(lambda:not Path('/proc',str(record['pid'])).exists(),seconds=13)
                session.connect_node('leased')
                self.assertNotEqual(resource.environment.sandbox.session.runtime_id,previous_runtime)
                self.assertEqual(session.team.dispatch('master','python',{'node_id':'leased','code':'print(42)'})['stdout'],'42\n')
            finally:session.close()
