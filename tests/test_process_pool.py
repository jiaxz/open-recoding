"""Focused Node ProcessPool contract, real workers and loopback SSH, no model API.

This test owns and removes only its unique child directory. It deliberately does
not import the global test cleanup helper: manual user Sessions may be present.
"""
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import json
import shutil
import tempfile
import time
import unittest
import uuid

from node.service import ExecutionService
from tools import definitions,execution_sources
from tools.registry import ToolRegistry
from tools.python import PythonTools
from tools.bash.service import BashTools
from tools.file_transfer.service import FileTransfers
from ssh_support import local_ssh

ROOT=Path(__file__).resolve().parents[1]
TERMINAL={'succeeded','failed','cancelled','timed_out','interrupted'}

class ProcessPoolTests(unittest.TestCase):
    def wait(self,predicate,timeout=10):
        deadline=time.monotonic()+timeout
        while True:
            value=predicate()
            if value:return value
            self.assertLess(time.monotonic(),deadline,'Operation exceeded test deadline')
            time.sleep(.025)

    def test_unified_pool_local_and_real_ssh_lifecycle(self):
        directory=Path(tempfile.mkdtemp(prefix='process-pool-refactor-',dir=ROOT/'sessions/tests'))
        try:
            with ExitStack() as resources:
                fixture=resources.enter_context(local_ssh(directory/'ssh'))
                workspace=directory/'workspace';workspace.mkdir()
                folder=workspace/'.recoding'/uuid.uuid4().hex;folder.mkdir(parents=True)
                actors={name:{'id':name,'status':'running','permissions':{},'epoch':1} for name in ('master','a','b')}
                notices=[]
                config={'execution_mode':'sandbox','network':True,'gpu':False,'tool_timeout':10,'max_output':16000,'non_builtin_tools':definitions()}
                service=ExecutionService(SimpleNamespace(id=uuid.uuid4().hex,workspace=workspace,folder=folder),config,
                    actor_info=actors.get,notify=lambda target,text,meta:notices.append((target,text,meta)) or uuid.uuid4().hex,
                    observe=lambda *args:None,adapters={'python':PythonTools,'bash':BashTools,'files':FileTransfers},
                    sources=execution_sources(),registry_factory=ToolRegistry)
                resources.callback(service.close);service.ensure()
                service.configure('remote',{'name':'Test SSH','ssh_target':fixture.alias,'workspace':str(fixture.remote),'ssh_config':str(fixture.ssh_config)})
                call=lambda who,tool,**fields:service.request(who,tool,fields)
                for node in ('local','remote'):
                    for kind in ('python','bash'):
                        with self.subTest(node=node,kind=kind):
                            for actor in ('a','b'):actors[actor].update(status='running',epoch=actors[actor]['epoch']+1)
                            process=call('a',kind+'_process',action='create',node_id=node,name='cooperation')['process']['id']
                            code=lambda text:{'code' if kind=='python' else 'command':text}
                            initial='value=41' if kind=='python' else 'value=41'
                            show='print(value+1)' if kind=='python' else 'printf "%s\\n" "$((value+1))"'
                            call('b',kind,process_id=process,**code(initial))
                            self.assertEqual(call('master',kind,process_id=process,**code(show))['stdout'],'42\n')
                            if node=='remote' and kind=='python':
                                backend=service.resources[node].environment.sandbox
                                runtime_id=backend.session.runtime_id
                                backend.session.close('Test transient disconnect')
                                self.assertEqual(call('b',kind,process_id=process,**code(show))['stdout'],'42\n')
                                self.assertEqual(backend.session.runtime_id,runtime_id)
                            self.assertIn(process,{p['id'] for p in call('b',kind+'_process',action='list',node_id=node)['processes']})
                            with self.assertRaisesRegex(ValueError,'Owner|owner'):call('b',kind+'_process',action='rename',process_id=process,name='forbidden')
                            self.assertEqual(call('a',kind+'_process',action='rename',process_id=process,name='renamed')['process']['name'],'renamed')
                            # Two callers share one authoritative FIFO; completion goes to submitter.
                            first=call('b',kind,process_id=process,mode='async',**code('import time;time.sleep(.25);value=50' if kind=='python' else 'sleep .25;value=50'))['execution']['id']
                            second=call('master',kind,process_id=process,mode='async',**code(show))['execution']['id']
                            self.wait(lambda:call('a',kind+'_execution',action='status',execution_id=second)['execution']['status'] in TERMINAL)
                            self.assertEqual(call('a',kind+'_execution',action='read',execution_id=second)['stdout'],'51\n')
                            self.wait(lambda:any(t=='b' and m.get('execution_id')==first for t,_,m in notices))
                            # Stopping a submitter cancels its work in someone else's process.
                            sleeping='import time;time.sleep(30)' if kind=='python' else 'sleep 30'
                            execution=call('b',kind,process_id=process,mode='async',**code(sleeping))['execution']['id']
                            self.wait(lambda:call('a',kind+'_execution',action='status',execution_id=execution)['execution']['status']=='running')
                            actors['b']['status']='stopping';service.stop_owner('b');actors['b']['status']='stopped'
                            self.wait(lambda:call('a',kind+'_execution',action='status',execution_id=execution)['execution']['status'] in TERMINAL)
                            self.assertEqual(call('a',kind,process_id=process,**code(show))['stdout'],'51\n')
                            actors['b'].update(status='running',epoch=actors['b']['epoch']+1)
                            # Owner Stop retires admission, preserves other's current execution,
                            # discards queued calls, then deletes the interpreter.
                            current=call('b',kind,process_id=process,mode='async',**code('import time;time.sleep(.5);print("finished")' if kind=='python' else 'sleep .5;printf "finished\\n"'))['execution']['id']
                            self.wait(lambda:call('master',kind+'_execution',action='status',execution_id=current)['execution']['status']=='running')
                            queued=call('master',kind,process_id=process,mode='async',**code('print("must not run")' if kind=='python' else 'echo "must not run"'))['execution']['id']
                            actors['a']['status']='stopping';service.stop_owner('a');actors['a']['status']='stopped'
                            with self.assertRaisesRegex(ValueError,'accepting'):call('b',kind,process_id=process,**code(show))
                            self.wait(lambda:call('b',kind+'_execution',action='status',execution_id=current)['execution']['status'] in TERMINAL)
                            self.assertEqual(call('b',kind+'_execution',action='read',execution_id=current)['stdout'],'finished\n')
                            self.assertEqual(call('b',kind+'_execution',action='status',execution_id=queued)['execution']['status'],'cancelled')
                            self.wait(lambda:any(p['id']==process and p['status']=='closed' for p in call('master',kind+'_process',action='list',node_id=node)['processes']))
                            # A new attempt is allowed; ownership transfer keeps state through old Owner Stop.
                            actors['a'].update(status='running',epoch=actors['a']['epoch']+1)
                            kept=call('a',kind+'_process',action='create',node_id=node,name='keep')['process']['id']
                            call('a',kind,process_id=kept,**code(initial))
                            client=getattr(service.resources[node],kind+'_pool')
                            client._rpc('stop_owner',owner='a',epoch=actors['a']['epoch']-1)
                            self.assertEqual(call('b',kind,process_id=kept,**code(show))['stdout'].strip(),'42')
                            call('a',kind+'_process',action='transfer',process_id=kept,new_owner='b')
                            actors['a']['status']='stopping';service.stop_owner('a');actors['a']['status']='stopped'
                            self.assertEqual(call('b',kind,process_id=kept,**code(show))['stdout'],'42\n')
                            call('master',kind+'_process',action='delete',process_id=kept)
                # Stateful SSH Shell uses the same generic queue and language driver.
                with local_ssh(workspace/'ssh-fixture') as shell_fixture:
                    config_path=shell_fixture.root/'sandbox_config'
                    config_path.write_text(shell_fixture.ssh_config.read_text().replace(str(shell_fixture.root),'/workspace/ssh-fixture'))
                    actors['b'].update(status='running',epoch=actors['b']['epoch']+1)
                    shell=call('b','bash_process',action='create',name='SSH Shell',
                        ssh_argv=['/usr/bin/ssh','-T','-F','/workspace/ssh-fixture/sandbox_config',shell_fixture.alias])['process']['id']
                    self.assertEqual(call('master','bash',process_id=shell,command='value=41;cd nested;echo $((value+1))')['stdout'],'42\n')
                    self.assertEqual(call('b','bash',process_id=shell,command='echo "$value";pwd')['stdout'],'41\n'+str(shell_fixture.remote/'nested')+'\n')
                    self.assertEqual(shell_fixture.log.read_text().count('Accepted publickey'),1)
                    call('master','bash_process',action='delete',process_id=shell)
        finally:shutil.rmtree(directory)

    def test_session_stop_resume_uses_node_owner_retirement(self):
        from session import Session,SavedSession
        directory=Path(tempfile.mkdtemp(prefix='process-pool-session-',dir=ROOT/'sessions/tests'))
        session=None
        try:
            session=Session.create(directory/'workspace','Process pool fixture',
                config={'demo':True,'network':False,'gpu':False,'tool_timeout':5})
            self.wait(lambda:session.master.status=='sleeping')
            sub=session.team.dispatch('master','create_agent',{'name':'Owner'})['agent_id']
            self.wait(lambda:session.team._agent(sub)['status']=='sleeping')
            call=lambda who,tool,**fields:session.team.dispatch(who,tool,fields)
            process=call(sub,'python_process',action='create',name='owned')['process']['id']
            completed=call(sub,'python',process_id=process,code='value=41;print(value)')['execution']['id']
            active=call('master','python',process_id=process,code='import time;time.sleep(.3);print(value+1)',mode='async')['execution']['id']
            self.wait(lambda:call('master','python_execution',action='status',execution_id=active)['execution']['status']=='running')
            call('master','stop_agent',agent_id=sub,reason='Lifecycle test')
            self.wait(lambda:session.team._agent(sub)['status']=='stopped')
            self.wait(lambda:session.python_pool.snapshot()['processes'][process]['status']=='closed')
            self.assertEqual(call('master','python_execution',action='read',execution_id=active)['stdout'],'42\n')
            call('master','resume_agent',agent_id=sub)
            self.wait(lambda:session.team._agent(sub)['status']=='sleeping')
            fresh=call(sub,'python_process',action='create',name='fresh')['process']['id']
            self.assertNotEqual(process,fresh)
            self.assertEqual(call(sub,'python',process_id=fresh,code='print("value" in globals())')['stdout'],'False\n')
            archive=session.store.folder
            backend=session.execution.environment.sandbox
            session.cancel()
            self.assertIn('closed',backend.session.request('check').get('error',''))
            self.assertIsNone(backend.session.process)
            self.assertTrue(all(p['status']=='closed' for p in session.python_pool.snapshot()['processes'].values()))
            self.assertEqual(SavedSession(archive).execution_output(sub,completed)['stdout'],'41\n')
        finally:
            if session:session.close()
            shutil.rmtree(directory)

    def test_owner_close_grace_and_foreign_unresponsive_cancellation(self):
        directory=Path(tempfile.mkdtemp(prefix='process-pool-grace-',dir=ROOT/'sessions/tests'))
        service=None
        try:
            workspace=directory/'workspace';workspace.mkdir()
            folder=workspace/'.recoding'/uuid.uuid4().hex;folder.mkdir(parents=True)
            actors={k:{'status':'running','permissions':{},'epoch':1} for k in ('master','a','b')}
            config={'execution_mode':'host','network':True,'gpu':True,'tool_timeout':5,'max_output':16000,'non_builtin_tools':definitions()}
            service=ExecutionService(SimpleNamespace(id=uuid.uuid4().hex,workspace=workspace,folder=folder),config,
                actor_info=actors.get,notify=lambda *args:uuid.uuid4().hex,observe=lambda *args:None,
                adapters={'python':PythonTools,'bash':BashTools,'files':FileTransfers},sources=execution_sources(),registry_factory=ToolRegistry)
            call=lambda who,tool,**fields:service.request(who,tool,fields)
            code='import signal,time;signal.signal(signal.SIGINT,signal.SIG_IGN);print("ready",flush=True);time.sleep(30)'
            for owner,submitter,should_close in [('a','b',True),('b','a',False)]:
                actors['a'].update(status='running',epoch=actors['a']['epoch']+1)
                pid=call(owner,'python_process',action='create')['process']['id']
                execution=call(submitter,'python',process_id=pid,code=code,mode='async')['execution']['id']
                self.wait(lambda:'ready' in call('master','python_execution',action='read',execution_id=execution)['stdout'])
                actors['a']['status']='stopping';service.stop_owner('a');actors['a']['status']='stopped'
                if should_close:
                    self.wait(lambda:call('master','python_execution',action='status',execution_id=execution)['execution']['status'] in TERMINAL,timeout=8)
                    self.assertTrue(call('master','python_execution',action='status',execution_id=execution)['execution']['memory_lost'])
                else:
                    self.wait(lambda:call('master','python_execution',action='status',execution_id=execution)['execution']['status']=='unresponsive',timeout=4)
                    self.assertEqual(call('b','python_process',action='list')['processes'][-1]['status'],'unresponsive')
                    call('master','python_process',action='delete',process_id=pid)
        finally:
            if service:service.close()
            shutil.rmtree(directory)

    def test_cancel_queued_temporary_uses_language_independent_lifecycle(self):
        from node.process_pool import ProcessPool
        directory=Path(tempfile.mkdtemp(prefix='process-pool-queued-',dir=ROOT/'sessions/tests'))
        class Driver:
            process=None
            def __init__(self,*args):self.closed=False;self.calls=0
            def start(self,*args):return {'pid':None}
            def execute(self,*args):self.calls+=1;return {'exit_code':0}
            def close(self):self.closed=True
        pool=ProcessPool({'cwd':str(directory),'env':{'RECODING_WORKSPACE':str(directory),'RECODING_EXECUTION_OWNER':uuid.uuid4().hex}}, {}, {'other_language':Driver})
        try:
            actor={'id':'a','epoch':1}
            with pool.lock:
                result=pool.request({'operation':'submit','language':'other_language','actor':actor,'code':'not executed','mode':'async'})
                identifier=result['execution']['id'];process=pool.processes[result['execution']['process_id']]
                cancelled=pool.request({'operation':'cancel','language':'other_language','actor':actor,'execution_id':identifier})
                self.assertEqual(cancelled['execution']['status'],'cancelled')
            self.wait(process['closed'].is_set)
            self.assertTrue(process['driver'].closed);self.assertEqual(process['driver'].calls,0)
        finally:pool.close();shutil.rmtree(directory)

if __name__=='__main__':unittest.main()
