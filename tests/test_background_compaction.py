"""A live Session keeps its loop, mailbox and Python memory during maintenance."""
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from multiagent.context_window import SUMMARY_SYSTEM
from session import Session,SessionStore
from model_support import local_model
from test_context_window import history
from workspace_support import temporary_workspace


def wait(test, timeout=12):
    deadline=time.monotonic()+timeout
    while not test():
        if time.monotonic()>deadline:raise AssertionError('Timed out waiting for offline Session fixture')
        time.sleep(.02)


class BackgroundCompactionTests(unittest.TestCase):
    def exercise(self,mode):
        with temporary_workspace(prefix='recoding-background-context-') as directory:
            root=Path(directory)
            source=history(24,4000)
            store=SessionStore.create(root/'workspace','background-'+mode,{})
            store.save_state({'messages':source,'iteration':24,'tool_count':24,'run_state':'stopped','location':{'phase':'tool'}})
            summary_started,release,python_done=threading.Event(),threading.Event(),threading.Event()
            normal=[];process_created=False;python_sent=False;check_sent=False
            memory_verified=threading.Event()
            def respond(body,number):
                nonlocal process_created,python_sent,check_sent
                if body['messages'][0]['content']==SUMMARY_SYSTEM:
                    self.assertNotIn('tools',body);self.assertNotIn('tool_choice',body)
                    summary_started.set();release.wait(10)
                    return {'role':'assistant','content':'' if mode=='failure' else 'BACKGROUND_MEMORY: old verification is saved; continue current user requirements.'}
                normal.append(body)
                def call(tool_name,**fields):return {'role':'assistant','tool_calls':[{'id':f'local-{number}','index':0,'type':'function','function':{'name':tool_name,'arguments':json.dumps(fields)}}]}
                results=[json.loads(m['content']) for m in body['messages'] if m['role']=='tool' and m['tool_call_id'].startswith('local-')]
                if not process_created:
                    process_created=True
                    return call('python_process',action='create',name='compaction-memory')
                process_id=next(item['process']['id'] for item in results if 'process' in item)
                if not python_sent:
                    python_sent=True
                    return call('python',process_id=process_id,code='sentinel=73; print(sentinel)')
                if any(item.get('exit_code')==0 and not item.get('error') and item.get('stdout')=='73\n' for item in results):python_done.set()
                if any(item.get('exit_code')==0 and not item.get('error') and item.get('stdout')=='73\nMEMORY_PRESERVED\n' for item in results):memory_verified.set()
                if not check_sent and any('VERIFY_MEMORY_AFTER_COMPRESSION' in str(m.get('content')) for m in body['messages']):
                    check_sent=True;return call('python',process_id=process_id,code="print(sentinel); print('MEMORY_PRESERVED')")
                return call('sleep',seconds=.05)
            with local_model(root/'model',respond) as model,patch.dict(os.environ,{
                    'RECODING_API_KEY':'offline-only','RECODING_BASE_URL':model.url,'SSL_CERT_FILE':str(model.cert),
                    'NO_PROXY':'127.0.0.1','no_proxy':'127.0.0.1','PYTHONUSERBASE':str(root/'empty-site')}):
                session=Session(store,{'network':False,'gpu':False,'api_timeout':10,'tool_timeout':5,
                                      'context_limits':{'grok-4.7':60000}})
                try:
                    self.assertTrue(summary_started.wait(10),session.snapshot(events=False))
                    self.assertTrue(python_done.wait(8),'Live loop must run Python while the summary waits')
                    worker=session.master.process.pid
                    count=len(normal)
                    receipt=session.submit('MESSAGE_DURING_BACKGROUND_COMPRESSION')
                    wait(lambda:len(normal)>count+1)
                    self.assertTrue(any('MESSAGE_DURING_BACKGROUND_COMPRESSION' in str(request['messages']) for request in normal))
                    operation=session.compaction.snapshot()[0]
                    if mode=='cancel':
                        self.assertTrue(session.compaction.cancel_request({'request_id':operation['request_id']})['cancelled'])
                    else:release.set()
                    wait(lambda:session.compaction.snapshot()[0]['status'] in {'succeeded','failed','cancelled'})
                    run_id=session.compaction.snapshot()[0]
                    self.assertEqual(run_id['status'],{'success':'succeeded','failure':'failed','cancel':'cancelled'}[mode])
                    self.assertEqual(session.master.process.pid,worker,'Compression cannot restart the live worker')
                    current=store.load_state()['messages']
                    self.assertTrue(any(m.get('_agent_message_id')==receipt['queued'] for m in current))
                    self.assertEqual(current[1:len(source)],source[1:],'Original signed history stays unchanged')
                    if mode=='success':
                        wait(lambda:any('BACKGROUND_MEMORY' in str(request['messages']) for request in normal))
                        self.assertTrue((store.folder/'context-window.json').exists())
                        adopted=next(i for i,request in enumerate(normal) if 'BACKGROUND_MEMORY' in str(request['messages']))
                        wait(lambda:len(normal)>=adopted+4 or session.master.status=='failed')
                        self.assertNotEqual(session.master.status,'failed',model.errors or session.master.error)
                        for request in normal[adopted:adopted+4]:
                            self.assertIn('BACKGROUND_MEMORY',str(request['messages']),
                                          'An open tool batch must not discard a committed summary')
                            self.assertLess(len(request['messages']),len(source))
                        self.assertTrue(any(event['type']=='model_end' and event.get('context',{}).get('compaction_generation',0)>0
                                            for event in session.master.events))
                    else:
                        self.assertFalse((store.folder/'context-window.json').exists())
                        self.assertFalse(any('BACKGROUND_MEMORY' in str(request['messages']) for request in normal))
                    session.submit('VERIFY_MEMORY_AFTER_COMPRESSION')
                    self.assertTrue(memory_verified.wait(8),'The same persistent Python process must return sentinel successfully')
                    self.assertEqual(session.master.process.pid,worker)
                    self.assertFalse(any(event['type']=='loop_stopped' for event in session.master.events))
                    if mode!='cancel':self.assertFalse(model.errors,model.errors)
                    (root/'report.json').write_text(json.dumps({'mode':mode,'status':run_id['status'],'worker_preserved':True,
                        'normal_requests':len(normal),'queued_message_preserved':True,'external_api_calls':0},indent=2))
                finally:
                    release.set();session.close()

    def test_success_adopts_summary_at_next_call_without_stopping_live_work(self):self.exercise('success')
    def test_cancel_preserves_context_and_runtime(self):self.exercise('cancel')
    def test_summary_failure_preserves_context_and_runtime(self):self.exercise('failure')
