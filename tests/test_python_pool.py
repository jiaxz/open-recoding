"""Targeted unified Pool integration: real sandbox, no external model or user data."""
import json,os,threading,time,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from multiagent.config import configuration
from node.packages import package_directory
from session import Session,SessionStore
from tools.python import PythonTools
from workspace_support import temporary_workspace
from model_support import local_model

A='a'*32;B='b'*32

class PythonPoolTests(unittest.TestCase):
    def setUp(self):
        self.workspace=temporary_workspace(prefix='python-pool-');self.root=Path(self.workspace.name)
        self.environment=patch.dict(os.environ,{'PYTHONUSERBASE':str(self.root/'empty-user-base')});self.environment.start();self.addCleanup(self.environment.stop)
        self.store=SessionStore.create(self.root,'Pool fixture',{})
        self.config=configuration({'network':False,'gpu':False,'tool_timeout':5,'max_output':16000})
        self.config['package_directory']=str(package_directory(self.root))
        self.actors={key:{'id':key,'status':'running','permissions':{}} for key in ('master',A,B)}
        self.notices=[];self.events=[]
        def notify(target,content,config):
            for item in self.notices:
                if config.get('execution_id') and item['target']==target and item['config']==config:return item['id']
            item={'id':uuid.uuid4().hex,'target':target,'content':content,'config':config};self.notices.append(item);return item['id']
        self.pool=PythonTools(self.store,self.config,actor_info=self.actors.get,notify=notify,observe=lambda kind,record:self.events.append((kind,record)))
        self.addCleanup(self.pool.close)
    def call(self,source,tool_name,**fields):return self.pool.request(source,tool_name,fields)
    def create(self,owner=A,name='work'):return self.call(owner,'python_process',action='create',name=name)['process']['id']
    def execute(self,source,pid,code,mode='sync',**fields):return self.call(source,'python',process_id=pid,code=code,mode=mode,**fields)
    def done(self,identifier,timeout=8):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            result=self.call(A if self.pool.data['executions'][identifier]['source']==A else self.pool.data['executions'][identifier]['source'],'python_execution',action='status',execution_id=identifier)['execution']
            if result['status'] in {'succeeded','failed','cancelled','timed_out','interrupted'}:return result
            time.sleep(.02)
        self.fail('Execution did not finish: '+str(result))
    def test_temporary_python_is_fresh_and_reclaimed_before_return(self):
        first=self.call(A,'python',code='value=42; print(value)')
        self.assertEqual(first['stdout'],'42\n',first)
        process_id=first['execution']['process_id']
        record=self.pool.data['processes'][process_id]
        self.assertEqual(record['lifetime'],'temporary');self.assertEqual(record['status'],'closed')
        second=self.call(A,'python',code='import os; print("value" in globals()); print(os.path.exists("/proc/'+str(record['pid'])+'"))')
        self.assertEqual(second['stdout'],'False\nFalse\n',second)
        self.assertNotEqual(second['execution']['process_id'],process_id)
        self.assertEqual(len(self.notices),0)
        persistent=self.create()
        self.execute(A,persistent,'kept=7')
        self.assertEqual(self.execute(A,persistent,'print(kept)')['stdout'],'7\n')

    def test_temporary_async_completion_contains_output_and_cleans_memory(self):
        result=self.call(A,'python',code='import time; time.sleep(.1); print("temporary-result")',mode='async')
        execution=self.done(result['execution']['id'])
        self.assertEqual(execution['status'],'succeeded')
        self.assertEqual(self.pool.data['processes'][execution['process_id']]['status'],'closed')
        until=time.monotonic()+3
        while not self.notices:
            self.assertLess(time.monotonic(),until);time.sleep(.01)
        self.assertEqual(len(self.notices),1)
        self.assertIn('stdout:\ntemporary-result',self.notices[0]['content'])
        output=self.call(A,'python_execution',action='read',execution_id=result['execution']['id'])
        self.assertEqual(output['stdout'],'temporary-result\n')

    def test_temporary_cancel_and_timeout_can_reclaim_unresponsive_interpreter(self):
        result=self.call(A,'python',code='import signal,time;signal.signal(signal.SIGINT,signal.SIG_IGN);print("ready",flush=True);time.sleep(30)',mode='async')
        identifier=result['execution']['id'];until=time.monotonic()+3
        while 'ready' not in self.call(A,'python_execution',action='read',execution_id=identifier)['stdout']:
            self.assertLess(time.monotonic(),until);time.sleep(.02)
        self.call(A,'python_execution',action='cancel',execution_id=identifier)
        record=self.done(identifier)
        self.assertEqual(record['status'],'cancelled')
        self.assertEqual(self.pool.data['processes'][record['process_id']]['status'],'closed')
        timed=self.call(A,'python',code='import time;time.sleep(30)',timeout=.05)
        self.assertEqual(timed['execution']['status'],'timed_out',timed)
        self.assertEqual(self.pool.data['processes'][timed['execution']['process_id']]['status'],'closed')

    def test_one_sandbox_shared_namespace_fifo_and_parallel_processes(self):
        pid=self.create();sandbox_pid=self.pool.sandbox.session.process.pid
        other=self.create(B,'independent');self.assertEqual(self.pool.sandbox.session.process.pid,sandbox_pid)
        self.execute(A,pid,'trace=[]; value=41')
        self.assertEqual(self.execute(B,pid,'print(value)')['stdout'],'41\n')
        slow=self.execute(A,pid,'import time\ntime.sleep(.5)\ntrace.append("A")',mode='async')['execution']['id']
        queued=self.execute(B,pid,'trace.append("B");value+=1',mode='async')['execution']['id']
        start=time.monotonic();parallel=self.execute(B,other,'print("parallel")')
        self.assertLess(time.monotonic()-start,.4,parallel)
        self.done(slow);self.done(queued)
        result=self.execute(B,pid,'print(value); print(trace)')
        self.assertEqual(result['stdout'],"42\n['A', 'B']\n",result)
        self.assertEqual(self.execute(B,other,'print("value" in globals())')['stdout'],'False\n')
        with self.assertRaisesRegex(ValueError,'Owner|owner'):self.call(B,'python_process',action='delete',process_id=pid)
        self.assertEqual(len([n for n in self.notices if n['config'].get('event')=='python_execution_finished']),2)
    def test_cancel_execution_preserves_namespace_and_removes_queued_code(self):
        pid=self.create();self.execute(A,pid,'kept=123')
        task=self.execute(A,pid,'import time\nwhile True:\n    check_cancel()\n    time.sleep(.02)',mode='async')['execution']['id']
        cancelled=self.call(A,'python_execution',action='cancel',execution_id=task)
        self.assertIn(cancelled['execution']['status'],{'cancelling','cancelled'})
        self.assertEqual(self.done(task)['status'],'cancelled')
        self.assertEqual(self.execute(A,pid,'print(kept)')['stdout'],'123\n')
        first=self.execute(A,pid,'import time;time.sleep(.3)',mode='async')['execution']['id']
        pending=self.execute(A,pid,'should_not_run=True',mode='async')['execution']['id']
        self.assertEqual(self.call(A,'python_execution',action='cancel',execution_id=pending)['execution']['status'],'cancelled')
        self.done(first)
        self.assertEqual(self.execute(A,pid,'print("should_not_run" in globals())')['stdout'],'False\n')
        self.assertEqual(self.pool.data['processes'][pid]['status'],'ready')
    def test_unresponsive_cancel_does_not_kill_or_block_other_process(self):
        pid=self.create();task=self.execute(A,pid,'import signal,time\nsignal.signal(signal.SIGINT,signal.SIG_IGN)\nprint("ready",flush=True)\ntime.sleep(30)',mode='async')['execution']['id']
        end=time.monotonic()+5
        while 'ready' not in self.call(A,'python_execution',action='read',execution_id=task)['stdout']:
            self.assertLess(time.monotonic(),end);time.sleep(.02)
        self.call(A,'python_execution',action='cancel',execution_id=task)
        end=time.monotonic()+5
        while self.pool.data['executions'][task]['status']!='unresponsive':
            self.assertLess(time.monotonic(),end);time.sleep(.03)
        self.assertEqual(self.pool.data['processes'][pid]['status'],'unresponsive')
        other=self.create(B)
        self.assertEqual(self.execute(B,other,'print("available")')['stdout'],'available\n')
        with self.assertRaisesRegex(ValueError,'not accepting'):self.execute(A,pid,'print("bad")')
        self.call(A,'python_process',action='delete',process_id=pid)
        self.assertEqual(self.pool.data['executions'][task]['status'],'cancelled')
        self.assertTrue(self.pool.data['executions'][task]['memory_lost'])
    def test_owner_stop_retires_after_other_submitters_current_call(self):
        pid=self.create();self.execute(A,pid,'old_state=99')
        task=self.execute(B,pid,'import time;time.sleep(.3);print(old_state)',mode='async')['execution']['id']
        until=time.monotonic()+3
        while self.call(B,'python_execution',action='status',execution_id=task)['execution']['status']!='running':
            self.assertLess(time.monotonic(),until);time.sleep(.01)
        self.actors[A]['status']='stopping';self.pool.stop_owner(A);self.actors[A]['status']='stopped'
        self.assertEqual(self.done(task)['status'],'succeeded')
        until=time.monotonic()+3
        while self.pool.snapshot()['processes'][pid]['status']!='closed':
            self.assertLess(time.monotonic(),until);time.sleep(.01)
        self.assertEqual(self.pool.data['processes'][pid]['owner'],A)
        self.actors[A].update(status='running',epoch=1);fresh=self.create()
        self.assertNotEqual(pid,fresh)
        self.assertEqual(self.execute(A,fresh,'print("old_state" in globals())')['stdout'],'False\n')

    def test_process_failure_is_local_and_deadline_requests_soft_cancellation(self):
        pid=self.create();other=self.create(B)
        crashed=self.execute(A,pid,'import os;os._exit(9)',mode='async')['execution']['id']
        self.assertEqual(self.done(crashed)['status'],'failed')
        self.assertEqual(self.execute(B,other,'print(42)')['stdout'],'42\n')
        self.execute(B,other,'kept=7')
        timed=self.execute(B,other,'import time;time.sleep(30)',mode='async',timeout=.05)['execution']['id']
        self.assertEqual(self.done(timed)['status'],'timed_out')
        self.assertEqual(self.execute(B,other,'print(kept)')['stdout'],'7\n')

    def test_open_access_preserves_tool_delegation_and_owner_management(self):
        pid=self.create();self.actors[B]['permissions']={'tools':['bash']}
        with self.assertRaisesRegex(ValueError,'delegated'):self.execute(B,pid,'print(1)')
        self.actors[B]['permissions']={}
        self.assertEqual(self.execute(B,pid,'print(1)')['stdout'],'1\n')
        with self.assertRaisesRegex(ValueError,'Owner|owner'):self.call(B,'python_process',action='rename',process_id=pid,name='bad')
        self.assertEqual(self.call(A,'python_process',action='rename',process_id=pid,name='renamed')['process']['name'],'renamed')

class PythonPoolLoopTests(unittest.TestCase):
    def test_real_sub_stop_retires_owned_process_and_saved_session_only_reads(self):
        from session import SavedSession
        root=Path(temporary_workspace(prefix='pool-session-stop-').name)
        with patch.dict(os.environ,{'PYTHONUSERBASE':str(root/'empty-user-base')}):
            session=Session.create(root/'project','Pool owner fixture',config={'demo':True,'network':False,'gpu':False,'tool_timeout':5})
            try:
                end=time.monotonic()+10
                while session.master.status!='sleeping':
                    self.assertLess(time.monotonic(),end);time.sleep(.02)
                sub=session.team.dispatch('master','create_agent',{'name':'Pool owner'})['agent_id']
                end=time.monotonic()+10
                while session.team._agent(sub)['status'] not in {'running','sleeping'}:
                    self.assertLess(time.monotonic(),end);time.sleep(.02)
                pool=session.python_pool
                pid=pool.request(sub,'python_process',{'action':'create','name':'shared'})['process']['id']
                completed=pool.request(sub,'python',{'process_id':pid,'code':'print("persisted-log")','mode':'sync'})['execution']['id']
                active=pool.request('master','python',{'process_id':pid,'code':'import time;time.sleep(30)','mode':'async'})['execution']['id']
                session.team.dispatch('master','stop_agent',{'agent_id':sub,'reason':'fixture Stop'})
                end=time.monotonic()+10
                while session.team._agent(sub)['status']!='stopped':
                    self.assertLess(time.monotonic(),end);time.sleep(.02)
                end=time.monotonic()+8  # Owner retirement includes a five-second grace plus transport refresh.
                while pool.snapshot()['processes'][pid]['status']!='closed':
                    self.assertLess(time.monotonic(),end);time.sleep(.02)
                self.assertEqual(pool.snapshot()['processes'][pid]['status'],'closed')
                self.assertEqual(pool.snapshot()['executions'][active]['status'],'cancelled')
                archive=session.store.folder
            finally:session.close()
            saved=SavedSession(archive)
            self.assertEqual(saved.python_resources()['processes'][pid]['status'],'closed')
            self.assertEqual(saved.execution_output(sub,completed)['stdout'],'persisted-log\n')
            resumed=Session.resume(archive,config={'demo':True,'network':False,'gpu':False})
            try:
                self.assertTrue(all(r['status']=='closed' for r in resumed.python_resources()['processes'].values()))
            finally:resumed.close()

    def test_async_completion_wakes_agent_and_system_contains_shared_usage_rules(self):
        root=Path(temporary_workspace(prefix='pool-loop-').name);finished=threading.Event();stage=[0];seen=[]
        def respond(body,number):
            seen.append(body)
            def calls(*items):return {'role':'assistant','tool_calls':[{'index':i,'id':f'pool-{number}-{i}','type':'function','function':{'name':name,'arguments':json.dumps(fields)}} for i,(name,fields) in enumerate(items)]}
            if stage[0]==0:
                stage[0]=1;return calls(('python_process',{'action':'create','name':'fixture'}))
            if stage[0]==1:
                stage[0]=2
                result=next(json.loads(m['content']) for m in reversed(body['messages']) if m['role']=='tool')
                return calls(('python',{'process_id':result['process']['id'],'code':'import time;time.sleep(.4);print("done")','mode':'async'}),('sleep',{'seconds':3600}))
            self.assertTrue(any('python execution ' in str(m.get('content','')) and 'succeeded' in str(m.get('content','')) for m in body['messages']))
            finished.set();return calls(('sleep',{'seconds':3600}))
        with local_model(root/'fixture-model',respond) as fixture,patch.dict(os.environ,{'RECODING_API_KEY':'fixture-only','SSL_CERT_FILE':str(fixture.cert),'NO_PROXY':'127.0.0.1','PYTHONUSERBASE':str(root/'empty-user-base')}):
            session=Session.create(root/'project','pool loop fixture',config={'base_url':fixture.url,'model':'fixture-model','network':False,'gpu':False,'api_timeout':5,'tool_timeout':5})
            try:
                self.assertTrue(finished.wait(12),str(fixture.errors)+str(session.master.error))
                self.assertFalse(fixture.errors)
                system=seen[0]['messages'][0]['content'];self.assertIn('SHORT cooperation',system)
                self.assertIn('All live Agents with the execution tool',system)
                names={item['function']['name'] for item in seen[0]['tools']}
                self.assertTrue({'python','python_process','python_execution'}<=names)
                self.assertFalse({'restart_python','shared_python','create_shared_python'}&names)
                records=session.python_pool.snapshot()['executions'];self.assertEqual(len(records),1)
                self.assertEqual(next(iter(records.values()))['status'],'succeeded')
            finally:session.close()

if __name__=='__main__':unittest.main()
