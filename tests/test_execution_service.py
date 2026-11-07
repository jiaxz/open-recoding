"""One real Session sandbox: concurrent tools, resource ownership and SSH handoff."""
import os,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from session import Session,SavedSession
from workspace_support import temporary_workspace

class ExecutionServiceTests(unittest.TestCase):
    network=False
    def setUp(self):
        self.root=Path(temporary_workspace(prefix='session-execution-').name)
        env=patch.dict(os.environ,{'PYTHONUSERBASE':str(self.root/'empty-site')});env.start();self.addCleanup(env.stop)
        self.session=Session.create(self.root/'workspace','Execution fixture',config={'demo':True,'network':self.network,'gpu':False,'tool_timeout':5})
        self.addCleanup(self.session.close)
        self.wait(lambda:self.session.master.status=='sleeping')
        self.a=self.sub('Builder');self.b=self.sub('Reader');self.service=self.session.execution
    def wait(self,predicate,timeout=10):
        until=time.monotonic()+timeout
        while not predicate():
            self.assertNotEqual(self.session.master.status,'failed',self.session.master.error)
            self.assertLess(time.monotonic(),until,'Operation exceeded deadline');time.sleep(.02)
    def sub(self,name):
        key=self.session.team.dispatch('master','create_agent',{'name':name})['agent_id']
        self.wait(lambda:self.session.team._agent(key)['status']=='sleeping');return key
    def call(self,source,tool_name,**fields):return self.session.team.dispatch(source,tool_name,fields)
    def create(self,source,kind,**fields):return self.call(source,kind+'_process',action='create',**fields)['process']['id']
    def execute(self,source,kind,pid,text,**fields):
        return self.call(source,kind,process_id=pid,**{'code' if kind=='python' else 'command':text},**fields)
    def done(self,source,kind,key):
        self.wait(lambda:self.call(source,kind+'_execution',action='status',execution_id=key)['execution']['status'] in {'succeeded','failed','cancelled','interrupted','timed_out'})
        return self.call(source,kind+'_execution',action='read',execution_id=key)
    def test_single_sandbox_all_tools_and_concurrent_control(self):
        facts=self.service.environment.facts();pid=self.create(self.a,'python')
        self.execute(self.a,'python',pid,'from pathlib import Path\nPath("/tmp/shared").write_text("python")')
        shell=self.create(self.b,'bash')
        self.assertEqual(self.execute(self.b,'bash',shell,'cat /tmp/shared')['stdout'],'python')
        # A synchronous call waits for its execution, never holds the shared RPC pipe.
        result=[];thread=threading.Thread(target=lambda:result.append(self.execute(self.a,'python',pid,'import time;time.sleep(.5);print("finished")')))
        thread.start();time.sleep(.1)
        start=time.monotonic();self.assertEqual(self.execute(self.b,'bash',shell,'printf responsive')['stdout'],'responsive')
        self.assertLess(time.monotonic()-start,.4);thread.join(3);self.assertFalse(thread.is_alive());self.assertEqual(result[0]['stdout'],'finished\n')
        # Command and image adapters use the same namespace and control transport.
        from test_images import png
        (self.session.directory/'p.png').write_bytes(png())
        image=self.call(self.b,'view_image',path='/workspace/p.png');self.assertIn('image',image)
        self.assertEqual(self.service.environment.facts()['sandbox_pid'],facts['sandbox_pid'])
        self.assertIs(self.service.python_pool.sandbox,self.service.environment.sandbox)
        self.assertEqual(self.service.environment.generation,1)
        self.assertFalse(hasattr(self.session.team, 'tool_services'))
        self.assertNotIn('tasks', self.session.master.snapshot(events=False))
        self.assertNotIn('executions', self.session.master.snapshot(events=False))
        self.assertTrue(all('run_id' in event and 'job' not in event for event in self.session.master.events if event['type'] == 'loop_start'))
    def test_transfer_preserves_running_and_fifo_work_and_routes_result(self):
        for kind in ('bash','python'):
            pid=self.create(self.a,kind,name='Transfer '+kind)
            self.execute(self.b,kind,pid,'print(0)' if kind=='python' else 'true')
            first=self.execute(self.a,kind,pid,'import time;time.sleep(.4);value=41' if kind=='python' else 'sleep .4; value=41',mode='async')['execution']['id']
            second=self.execute(self.b,kind,pid,'value+=1;print(value)' if kind=='python' else 'value=$((value+1));printf %s "$value"',mode='async')['execution']['id']
            self.call(self.a,kind+'_process',action='transfer',process_id=pid,new_owner=self.b)
            # Old owner cannot manage anymore; Master can, without taking ownership.
            with self.assertRaisesRegex(ValueError,'Owner|owner'):self.call(self.a,kind+'_process',action='delete',process_id=pid)
            self.assertEqual(self.done('master',kind,second)['stdout'].strip(),'42')
            self.done('master',kind,first)
            data=getattr(self.service,kind+'_pool').snapshot();self.assertEqual(data['processes'][pid]['creator'],self.a)
            self.assertEqual(data['processes'][pid]['owner'],self.b)
            self.wait(lambda:any(m.get('target')==self.b and m.get('configuration',{}).get('execution_id')==second for m in self.session.team.data['messages']))
            self.assertEqual(self.execute('master',kind,pid,'print(value)' if kind=='python' else 'printf %s "$value"')['stdout'].strip(),'42')
        self.call('master','stop_agent',agent_id=self.a,reason='Owner handoff fixture')
        self.wait(lambda:self.session.team._agent(self.a)['status']=='stopped')
        for kind in ('bash','python'):
            records=getattr(self.service,kind+'_pool').snapshot()['processes'].values()
            self.assertTrue(all(r['owner']==self.b and r['status']!='closed' for r in records))
        self.assertEqual(len(self.call(self.b,'pool_status')['processes']),2)
    def test_cancel_process_creation_reclaims_only_that_resource(self):
        cancelled=threading.Event();errors=[]
        def create():
            try:self.service.request(self.a,'bash_process',{'action':'create','ssh_argv':['/usr/bin/python3','-c','import time;time.sleep(30)'],'timeout':30},cancelled=cancelled.is_set)
            except ValueError as error:errors.append(str(error))
        thread=threading.Thread(target=create);thread.start()
        self.wait(lambda:bool(self.service.bash_pool.snapshot()['processes']))
        cancelled.set();thread.join(3);self.assertFalse(thread.is_alive());self.assertTrue(errors)
        self.assertTrue(all(r['status']=='closed' for r in self.service.bash_pool.snapshot()['processes'].values()))
        self.assertEqual(self.call(self.b,'bash',command='printf alive')['stdout'],'alive')

    def test_master_worker_lifecycle_and_sync_transfer_do_not_destroy_resources(self):
        for kind in ('bash','python'):
            pid=self.create(self.a,kind)
            reply=[]
            thread=threading.Thread(target=lambda:reply.append(self.execute(self.a,kind,pid,'import time;time.sleep(.3);kept=42;print(kept)' if kind=='python' else 'sleep .3;kept=42;printf %s "$kept"')))
            thread.start()
            pool=getattr(self.service,kind+'_pool')
            self.wait(lambda:any(r['process_id']==pid and r['status'] in {'queued','running'} for r in pool.snapshot()['executions'].values()))
            self.call('master',kind+'_process',action='transfer',process_id=pid,new_owner=self.b)
            thread.join(4);self.assertFalse(thread.is_alive());self.assertEqual(reply[0]['stdout'].strip(),'42')
        sandbox=self.service.environment.sandbox.session.process
        self.session.master.cancel();self.wait(lambda:self.session.master.status=='stopped')
        self.assertIsNone(sandbox.poll())
        self.assertTrue(all(self.session.team._agent(key)['status']=='sleeping' for key in (self.a,self.b)))
        self.session.start();self.wait(lambda:self.session.master.status=='sleeping')
        self.assertEqual(self.service.environment.sandbox.session.process.pid,sandbox.pid)
        self.assertEqual(len(self.call('master','pool_status')['processes']),2)

    def test_owner_stop_retirement_session_stop_and_read_only_logs(self):
        for kind in ('bash','python'):
            pid=self.create(self.a,kind)
            self.execute(self.a,kind,pid,'value=42' if kind=='python' else 'value=42')
            self.execute(self.a,kind,pid,'import time;time.sleep(.2);print(value)' if kind=='python' else 'sleep .2;printf %s "$value"',mode='async')
        self.call('master','stop_agent',agent_id=self.a,reason='Sub stopped')
        self.wait(lambda:all(r['status']=='closed' for pool in (self.service.python_pool,self.service.bash_pool) for r in pool.snapshot()['processes'].values()))
        status=self.call('master','pool_status');self.assertEqual(len(status['processes']),0)
        saved=[]
        for kind in ('bash','python'):
            data=getattr(self.service,kind+'_pool').snapshot()
            key=next(iter(data['executions']))
            self.done('master',kind,key)
            saved.append((kind,key))
        archive=self.session.store.folder;sandbox=self.service.environment.sandbox.session.process
        self.session.cancel();self.assertIsNotNone(sandbox.poll())
        for pool in (self.service.python_pool,self.service.bash_pool):self.assertTrue(all(r['status'] in {'closed','failed'} for r in pool.snapshot()['processes'].values()))
        view=SavedSession(archive)
        live = self.session.list_executions()['executions']
        self.assertEqual(view.list_executions()['executions'], live)
        before = {kind: (archive / (kind + '-pool.json')).read_bytes() for kind in ('bash', 'python')}
        for kind,key in saved:
            result = view.execution_output('master',key)
            self.assertIn('stdout',result)
            self.assertIn('execution',result)
            self.assertNotIn('task',result)
            self.assertNotIn('execution_id',result)
        self.assertEqual({kind: (archive / (kind + '-pool.json')).read_bytes() for kind in ('bash', 'python')}, before)
        self.wait(lambda:self.session.master.status=='stopped')
        self.session.start();self.wait(lambda:self.session.master.status=='sleeping')
        self.assertNotEqual(self.service.environment.sandbox.session.process.pid,sandbox.pid)
        self.assertEqual(self.call('master','pool_status')['processes'],[])

class SharedSSHTests(ExecutionServiceTests):
    network=True
    # Exercise only the SSH path here, not all inherited cases again.
    test_single_sandbox_all_tools_and_concurrent_control=None
    test_transfer_preserves_running_and_fifo_work_and_routes_result=None
    test_owner_stop_retirement_session_stop_and_read_only_logs=None
    test_master_worker_lifecycle_and_sync_transfer_do_not_destroy_resources=None
    test_cancel_process_creation_reclaims_only_that_resource=None
    def test_real_ssh_connection_survives_transfer_and_old_owner_stop(self):
        from ssh_support import local_ssh
        with local_ssh(self.session.directory/'ssh') as fixture:
            prefix=['/usr/bin/ssh','-T','-p',str(fixture.port),'-i','/workspace/ssh/client-key','-o','IdentitiesOnly=yes','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile=/workspace/ssh/known_hosts',fixture.user+'@127.0.0.1']
            pid=self.create(self.a,'bash',name='Remote SSH',ssh_argv=prefix)
            original=self.execute(self.a,'bash',pid,'value=41; cd nested; printf %s $$')['stdout']
            self.call('master','bash_process',action='transfer',process_id=pid,new_owner=self.b)
            task=self.execute(self.b,'bash',pid,'sleep .3;value=$((value+1));printf %s "$value"',mode='async')['execution']['id']
            self.call('master','bash_process',action='transfer',process_id=pid,new_owner=self.b)
            self.call('master','stop_agent',agent_id=self.a,reason='Transferred SSH')
            self.assertEqual(self.done(self.b,'bash',task)['stdout'],'42')
            result=self.execute(self.b,'bash',pid,'printf "%s:%s:" $$ "$value";pwd')['stdout']
            self.assertEqual(result,original+':42:'+str(fixture.remote/'nested')+'\n')
            self.assertEqual(fixture.log.read_text().count('Accepted publickey'),1)
            self.call('master','bash_process',action='delete',process_id=pid)

if __name__=='__main__':unittest.main()
