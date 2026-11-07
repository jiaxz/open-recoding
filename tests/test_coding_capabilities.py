"""Focused sandbox checks for reliable edits, repository navigation and installed Bash execution."""

import hashlib
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from sandbox_support import Sandbox
from multiagent.tools import HostTools
from tools import definitions
from model_support import local_model
from session import Session
from workspace_support import temporary_workspace


class CodingCapabilitiesTests(unittest.TestCase):
    def setUp(self):
        self.workspace=temporary_workspace(prefix='coding-capabilities-')
        self.root=Path(self.workspace.name)
        with patch.dict(os.environ,{'PYTHONUSERBASE':str(self.root/'empty-user-base')}):
            self.sandbox=Sandbox(self.root,network=False,gpu=False,timeout=5,non_builtin_tools=definitions())
        self.addCleanup(self.sandbox.close)

    def call(self,tool_name,**fields):return self.sandbox.execute(tool_name,json.dumps(fields))

    def done(self,identifier,timeout=5):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            result=self.call('bash_execution',action='read',execution_id=identifier,max_bytes=64000)
            if result.get('execution',{}).get('status') in {'succeeded','failed','cancelled','timed_out','interrupted'}:return result
            time.sleep(.02)
        self.fail('Job did not finish: '+repr(result))

    def test_revision_checks_patch_journal_undo_and_parallel_conflicts(self):
        original=b'def greet(name):\r\n    return "Hi " + name\r\n'
        path=self.root/'hello.py';path.write_bytes(original);path.chmod(0o755)
        first=self.call('read_file',path='hello.py')['data']
        self.assertEqual(first['sha256'],hashlib.sha256(original).hexdigest())
        update={'operation':'modify','path':'hello.py','expected_sha256':first['sha256'],
                'patch':'@@ -1,2 +1,2 @@\n def greet(name):\n-    return "Hi " + name\n+    return "Hello " + name\n'}
        edited=self.call('edit_files',changes=[update])
        self.assertEqual(edited['exit_code'],0,edited)
        journal=edited['data']['journal_id']
        self.assertIn(b'Hello ',path.read_bytes());self.assertIn(b'\r\n',path.read_bytes())
        self.assertEqual(path.stat().st_mode&0o777,0o755)
        stale=self.call('edit_files',changes=[update])
        self.assertEqual(stale['data']['error'],'edit_conflict',stale)
        undone=self.call('edit_files',undo_id=journal)
        self.assertEqual(undone['exit_code'],0,undone);self.assertEqual(path.read_bytes(),original)
        # Two independent Agent sandboxes share the same short edit lock.
        with patch.dict(os.environ,{'PYTHONUSERBASE':str(self.root/'empty-user-base')}):
            other=Sandbox(self.root,network=False,gpu=False,timeout=5,non_builtin_tools=definitions())
        self.addCleanup(other.close)
        barrier=threading.Barrier(2);results=[]
        def writer(sandbox,value):
            barrier.wait();change={'path':'hello.py','expected_sha256':first['sha256'],'content':value}
            results.append(sandbox.execute('edit_files',json.dumps({'changes':[change]})))
        threads=[threading.Thread(target=writer,args=(s,'value = '+str(i)+'\n')) for i,s in enumerate((self.sandbox,other))]
        for thread in threads:thread.start()
        for thread in threads:thread.join(10);self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(result['data']['ok'] for result in results),[False,True],results)
        path.write_text('external change\n')
        rejected=self.call('edit_files',undo_id=next(result['data']['journal_id'] for result in results if result['data']['ok']))
        self.assertEqual(rejected['data']['error'],'undo_conflict',rejected)
        self.assertEqual(path.read_text(),'external change\n')

    def test_navigation_and_lazy_index_follow_current_code(self):
        (self.root/'module.py').write_text('class First:\n    def value(self):\n        return 1\n')
        search=self.call('repo_query',mode='search',query='return 1')['data']
        self.assertEqual(search['results'][0]['line'],3,search)
        outline=self.call('repo_query',mode='outline',path='module.py')['data']
        self.assertEqual([r['name'] for r in outline['results']],['First','First.value'],outline)
        (self.root/'module.py').write_text('class Second:\n    pass\n')
        changed=self.call('repo_query',mode='symbol',query='Second')['data']
        self.assertEqual(changed['results'][0]['name'],'Second',changed)
        self.assertFalse(changed['semantic_references'])
        escaped=self.call('read_file',path='../outside.txt')
        self.assertNotEqual(escaped['exit_code'],0)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith('.')],['.recoding'])

    def test_fresh_bash_calls_use_explicit_env_and_non_builtin_permissions(self):
        self.call('bash',command='export JOB_EXPORTED=yes; ordinary=private')
        bash=self.call('bash',mode='async',env={'JOB_EXPORTED':'explicit'},command='printf "%s:%s" "$JOB_EXPORTED" "${ordinary-unset}"; sleep .1; printf done')
        shell=self.done(bash['execution']['id'])
        self.assertEqual(shell['stdout'],'explicit:unsetdone',shell)
        again=self.call('bash_execution',action='read',execution_id=bash['execution']['id'],stdout_offset=shell['stdout_offset'],stderr_offset=shell['stderr_offset'])
        self.assertEqual(again['stdout'],'')
        guarded=HostTools(lambda *_:None,lambda:[],[])
        self.assertIn('not installed',guarded.execute('bash','{"command":"true"}')['error'])

    def test_job_stdin_timeout_and_persisted_logs(self):
        bash=self.call('bash',mode='async',command='read line; printf "reply:%s" "$line"',keep_stdin_open=True)
        self.assertEqual(self.call('bash_execution',action='write',execution_id=bash['execution']['id'],text='hello\n',close_stdin=True).get('error'),None)
        self.assertEqual(self.done(bash['execution']['id'])['stdout'],'reply:hello')
        timed=self.call('bash',mode='async',command='sleep 30',timeout=.1)
        self.assertEqual(self.done(timed['execution']['id'])['execution']['status'],'timed_out')
        large=self.call('bash',mode='async',command="/usr/bin/python3 -c 'print(\"x\"*40000)' ")
        full=self.done(large['execution']['id'])
        self.assertEqual(len(full['stdout']),40001,full)
        owner=self.sandbox.execution_owner
        self.sandbox.close()
        with patch.dict(os.environ,{'PYTHONUSERBASE':str(self.root/'empty-user-base')}):
            reopened=Sandbox(self.root,network=False,gpu=False,execution_owner=owner,non_builtin_tools=definitions())
        self.addCleanup(reopened.close)
        old=reopened.execute('bash_execution',json.dumps({'action':'read','execution_id':large['execution']['id'],'max_bytes':64000}))
        self.assertEqual(old['execution']['status'],'succeeded',old)
        self.assertEqual(old['stdout'],full['stdout'])


    def test_large_structured_responses_remain_complete_and_paginated(self):
        content=('标记'+('数'*597)+'\n')*500
        (self.root/'large.txt').write_text(content)
        read=self.call('read_file',path='large.txt',max_chars=64000)
        self.assertFalse(read['truncated'],read)
        self.assertEqual(len(read['data']['content']),64000)
        self.assertTrue(read['data']['partial_last_line'])
        self.assertEqual(read['data']['sha256'],hashlib.sha256(content.encode()).hexdigest())
        found=self.call('repo_query',mode='search',path='large.txt',query='标记',limit=500)
        self.assertFalse(found['truncated'],found)
        result=found['data'];self.assertTrue(result['response_limited'],result)
        self.assertTrue(result['has_more']);self.assertLess(len(result['results']),500)
        following=self.call('repo_query',mode='search',path='large.txt',query='标记',limit=500,offset=result['next_offset'])['data']
        self.assertEqual(following['results'][0]['line'],result['results'][-1]['line']+1)

    def test_partial_edit_journal_can_restore_without_overwriting_later_changes(self):
        (self.root/'first.txt').write_text('before')
        locked=self.root/'locked';locked.mkdir();(locked/'second.txt').write_text('before')
        changes=[{'path':path,'expected_sha256':hashlib.sha256(b'before').hexdigest(),'content':'after'} for path in ['first.txt','locked/second.txt']]
        locked.chmod(0o555)
        try:result=self.call('edit_files',changes=changes)['data']
        finally:locked.chmod(0o755)
        self.assertFalse(result['ok'],result)
        self.assertTrue(result['partial'],result)
        self.assertEqual(result['committed'],['first.txt'])
        restored=self.call('edit_files',undo_id=result['journal_id'])
        self.assertEqual(restored['exit_code'],0,restored)
        self.assertEqual((self.root/'first.txt').read_text(),'before')
        self.assertEqual((locked/'second.txt').read_text(),'before')
        created=self.call('edit_files',changes=[{'operation':'create','path':'new.txt','expected_sha256':'absent','content':'new'}])['data']
        renamed=self.call('edit_files',changes=[{'operation':'rename','path':'new.txt','new_path':'renamed.txt','expected_sha256':created['revisions']['new.txt']}])['data']
        self.assertTrue(renamed['ok'],renamed)
        self.assertFalse((self.root/'new.txt').exists())
        removed=self.call('edit_files',changes=[{'operation':'delete','path':'renamed.txt','expected_sha256':renamed['revisions']['renamed.txt']}])
        self.assertTrue(removed['data']['ok'],removed)
        self.assertEqual(self.call('edit_files',undo_id=removed['data']['journal_id'])['exit_code'],0)
        self.assertEqual((self.root/'renamed.txt').read_text(),'new')


    def test_persistent_bash_completion_wakes_agent_with_output_once(self):
        stage=[0];finished=threading.Event();pid=[None]
        def respond(body,number):
            def calls(*entries):
                return {'role':'assistant','tool_calls':[{'index':i,'id':f'bash-pool-{number}-{i}','type':'function','function':{'name':name,'arguments':json.dumps(fields)}} for i,(name,fields) in enumerate(entries)]}
            if stage[0]==0:
                stage[0]=1;return calls(('bash_process',{'action':'create','name':'stateful fixture'}))
            if stage[0]==1:
                result=next(json.loads(m['content']) for m in reversed(body['messages']) if m['role']=='tool')
                pid[0]=result['process']['id'];stage[0]=2
                return calls(('bash',{'process_id':pid[0],'command':'answer=41'}))
            if stage[0]==2:
                stage[0]=3
                return calls(('bash',{'process_id':pid[0],'command':'sleep .3; printf %s "$((answer+1))"','mode':'async'}),('sleep',{'seconds':3600}))
            self.assertTrue(any('stdout:\n42' in str(m.get('content','')) for m in body['messages']),body)
            finished.set();return calls(('sleep',{'seconds':3600}))
        with local_model(self.root/'persistent-model',respond) as fixture,patch.dict(os.environ,{'RECODING_API_KEY':'fixture-only','SSL_CERT_FILE':str(fixture.cert),'NO_PROXY':'127.0.0.1','PYTHONUSERBASE':str(self.root/'empty-user-base')}):
            session=Session.create(self.root/'persistent-project','persistent shell fixture',config={'base_url':fixture.url,'model':'fixture-model','network':False,'gpu':False,'api_timeout':5,'tool_timeout':5})
            try:
                self.assertTrue(finished.wait(10),str(fixture.errors)+str(session.master.error))
                self.assertFalse(fixture.errors,fixture.errors)
                notices=[m for m in session.team.data['messages'] if m.get('configuration',{}).get('event')=='bash_execution_finished']
                self.assertEqual(len(notices),1,notices)
                self.assertIn('stdout:\n42',notices[0]['content'])
            finally:session.close()

    def test_job_completion_wakes_the_real_agent_through_its_inbox(self):
        stage=[0];finished=threading.Event();seen=[]
        def respond(body,number):
            seen.append(body)
            def tool(name,**fields):return {"role":"assistant","tool_calls":[{"index":0,"id":"job-fixture-"+str(number),"type":"function","function":{"name":name,"arguments":json.dumps(fields)}}]}
            if stage[0]==0:
                stage[0]=1
                names={item['function']['name'] for item in body['tools']}
                self.assertTrue({'bash','bash_execution','view_image'}<=names)
                self.assertFalse({'start_job','read_job','list_jobs','stop_job','write_job'}&names)
                return tool('bash',command='printf synchronous')
            if stage[0]==1:
                stage[0]=2
                return tool('bash',mode='async',command='sleep .5; printf job-finished')
            if stage[0]==2:
                stage[0]=3
                return tool('sleep',seconds=3600)
            self.assertTrue(any("bash execution " in str(message.get("content","")) and "succeeded" in str(message.get("content","")) for message in body["messages"]),body)
            finished.set()
            return tool("sleep",seconds=3600)
        with local_model(self.root/"fixture-model",respond) as fixture,patch.dict(os.environ,{"RECODING_API_KEY":"local-fixture","SSL_CERT_FILE":str(fixture.cert),"NO_PROXY":"127.0.0.1","PYTHONUSERBASE":str(self.root/"empty-user-base")}):
            session=Session.create(self.root/"project","job wake fixture",config={"base_url":fixture.url,"model":"fixture-model","network":False,"gpu":False,"api_timeout":5,"tool_timeout":5})
            try:
                self.assertTrue(finished.wait(10),str(fixture.errors)+str(session.master.error))
                self.assertFalse(fixture.errors,fixture.errors)
                tasks=session.list_executions()["executions"]
                self.assertEqual(tasks[0]["status"],"succeeded",tasks)
                self.assertEqual(session.execution_output("master",tasks[0]["id"])["stdout"],"job-finished")
                # Team-backed mail lives in its injected records rather than mailbox.data.
                notices=[item for item in session.team.data['messages'] if item.get('configuration',{}).get('event')=='bash_execution_finished']
                self.assertEqual(len(notices),1)
            finally:session.close()



if __name__=='__main__':unittest.main()
