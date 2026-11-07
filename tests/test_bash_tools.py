"""Real sandbox checks for stateless Bash, concurrent execution and local tools."""
import json
import os
from pathlib import Path
import shlex
import time
import unittest
from unittest.mock import patch

from multiagent.prompts import TOOLS
from multiagent.tools import HostTools
from sandbox_support import Sandbox
from tools import definitions
from workspace_support import temporary_workspace

TERMINAL={'succeeded','failed','cancelled','timed_out','interrupted'}

class BashToolsTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(temporary_workspace(prefix='fresh-bash-').name)
        with patch.dict(os.environ,{'PYTHONUSERBASE':str(self.root/'empty-site')}):
            self.sandbox=Sandbox(self.root,timeout=3,network=False,gpu=False,non_builtin_tools=definitions())
        self.addCleanup(self.sandbox.close)

    def call(self,tool_name='bash',**fields):
        return self.sandbox.execute(tool_name,json.dumps(fields))

    def done(self,identifier):
        until=time.monotonic()+5
        while time.monotonic()<until:
            result=self.call('bash_execution',action='read',execution_id=identifier,max_bytes=64000)
            if result.get('execution',{}).get('status') in TERMINAL:return result
            time.sleep(.02)
        self.fail('Execution did not finish: '+repr(result))

    def test_new_shells_share_sandbox_files_but_not_state(self):
        (self.root/'nested').mkdir()
        first=self.call(command="value=41; fn(){ echo function; }; export EXPORTED=yes; set +o pipefail; cd /tmp; printf kept > shared-file; echo $$")
        second=self.call(command='pwd; printf "%s:%s:%s" "${value-unset}" "${EXPORTED-unset}" "$(cat /tmp/shared-file)"; type fn')
        self.assertNotEqual(first['execution']['id'],second['execution']['id'])
        self.assertNotEqual(first['execution']['process_id'],second['execution']['process_id'])
        self.assertTrue(first['stdout'].strip().isdigit())
        self.assertEqual(first['session_state']['pid'],second['session_state']['pid'])
        self.assertIn('/workspace\nunset:unset:kept',second['stdout'])
        self.assertNotEqual(second['exit_code'],0)
        explicit=self.call(command='pwd; printf %s "$CHOICE"',cwd='nested',env={'CHOICE':'中文'})
        self.assertEqual(explicit['stdout'],'/workspace/nested\n中文')
        self.assertNotEqual(self.call(command='false | true')['exit_code'],0)
        self.assertEqual(self.call(command='exit 7')['exit_code'],7)
        self.assertEqual(self.call(command='printf healthy')['stdout'],'healthy')

    def test_async_calls_run_concurrently_and_supervisor_stays_usable(self):
        first=self.call(mode='async',command='touch /tmp/first-ready; while [ ! -e /tmp/second-ready ]; do sleep .02; done; printf first')
        second=self.call(mode='async',command='touch /tmp/second-ready; while [ ! -e /tmp/first-ready ]; do sleep .02; done; printf second')
        self.assertEqual(self.call(command='printf foreground')['stdout'],'foreground')
        self.assertEqual(self.done(first['execution']['id'])['stdout'],'first')
        self.assertEqual(self.done(second['execution']['id'])['stdout'],'second')

    def test_cancel_and_timeout_leave_other_executions_and_sandbox_alive(self):
        first=self.call(mode='async',command='trap "" TERM; sleep 30; touch BAD')
        second=self.call(mode='async',command='sleep .7; printf survivor')
        stopped=self.call('bash_execution',action='cancel',execution_id=first['execution']['id'])
        self.assertIn(stopped['execution']['status'],{'cancelling','cancelled'},stopped)
        self.assertEqual(self.done(first['execution']['id'])['execution']['status'],'cancelled')
        self.assertEqual(self.done(second['execution']['id'])['stdout'],'survivor')
        timed=self.call(command='sleep 30; touch LATE',timeout=.05)
        self.assertTrue(timed['timed_out'],timed)
        self.assertFalse((self.root/'BAD').exists());self.assertFalse((self.root/'LATE').exists())
        self.assertEqual(self.call(command='printf alive')['stdout'],'alive')

    def test_bounded_sync_output_preserves_full_logs_and_utf8_cursors(self):
        self.sandbox.max_output=100
        text='你好世界'*200
        result=self.call(command='python3 -c '+shlex.quote('print('+repr(text)+',end="")'))
        self.assertTrue(result['truncated'],result)
        self.assertLessEqual(len(result['stdout'].encode())+len(result['stderr'].encode()),100)
        stdout='';offset=0
        while True:
            page=self.call('bash_execution',action='read',execution_id=result['execution']['id'],stdout_offset=offset,max_bytes=128)
            stdout+=page['stdout'];offset=page['stdout_offset']
            if not page['stdout_has_more']:break
        self.assertEqual(stdout,text)

    def test_stdin_defaults_to_eof_and_optional_pipe_is_explicit(self):
        eof=self.call(command='if read line; then echo unexpected; else echo eof; fi')
        self.assertEqual(eof['stdout'],'eof\n')
        task=self.call(mode='async',command='read line; printf "reply:%s" "$line"',keep_stdin_open=True)
        written=self.call('bash_execution',action='write',execution_id=task['execution']['id'],text='hello\n',close_stdin=True)
        self.assertTrue(written['stdin_closed'],written)
        self.assertEqual(self.done(task['execution']['id'])['stdout'],'reply:hello')
        self.assertIn('error',self.call(command='read line',keep_stdin_open=True))

    def test_agent_sandbox_close_reclaims_processes_and_resume_only_reads_logs(self):
        task=self.call(mode='async',command='sleep 30')
        until=time.monotonic()+3
        record=task['execution']
        while not record.get('pid') and time.monotonic()<until:
            record=self.call('bash_execution',action='status',execution_id=task['execution']['id'])['execution']
        pid=self.sandbox.session.find_process(record['pid'])
        owner=self.sandbox.execution_owner
        self.sandbox.close()
        with self.assertRaises(ProcessLookupError):os.kill(pid,0)
        with patch.dict(os.environ,{'PYTHONUSERBASE':str(self.root/'empty-site')}):
            reopened=Sandbox(self.root,timeout=3,network=False,gpu=False,execution_owner=owner,non_builtin_tools=definitions())
        self.addCleanup(reopened.close)
        old=reopened.execute('bash_execution',json.dumps({'action':'status','execution_id':task['execution']['id']}))
        self.assertIn(old['execution']['status'],TERMINAL)
        self.assertFalse(old['execution'].get('status')=='running')
        other=reopened.execute('bash',json.dumps({'command':'printf new'}))
        self.assertNotEqual(other['execution']['id'],task['execution']['id'])

    def test_bash_and_view_image_are_optional_with_schema_and_permissions(self):
        self.assertEqual(TOOLS,[])
        plain=Sandbox(self.root,network=False,gpu=False)
        self.addCleanup(plain.close)
        for name in ('bash','bash_execution','view_image','start_job','restart_python'):
            self.assertIn('error',plain.execute(name,'{}'))
        self.assertIsNone(plain.session.process)
        executor=HostTools(lambda *_:None,lambda:[],[])
        self.assertIn('not installed',executor.execute('bash','{"command":"true"}')['error'])
        for fields in ({'command':'true','env':{'A':42}}, {'command':'true','mode':'invalid'}, {'command':'true','timeout':-1}):
            self.assertIn('error',self.call(**fields))
        self.assertIn('error',self.call('bash_execution',action='read'))

class BashPoolTests(unittest.TestCase):
    setUp=BashToolsTests.setUp
    call=BashToolsTests.call
    done=BashToolsTests.done

    def create(self,**fields):
        result=self.call('bash_process',action='create',**fields)
        self.assertIn('process',result,result)
        return result['process']['id']

    def test_persistent_state_fifo_isolation_and_temporary_default(self):
        pid=self.create(name='stateful')
        other=self.create(name='independent')
        self.call(process_id=pid,command='value=41; export SHARED=yes; mkdir -p /tmp/work; cd /tmp/work; fn(){ printf function; }; trace=')
        first=self.call(process_id=pid,command='sleep .2; trace=A',mode='async')
        second=self.call(process_id=pid,command='trace+=B; value=$((value+1)); printf queued',mode='async')
        self.assertEqual(self.call(process_id=other,command='printf independent')['stdout'],'independent')
        self.assertEqual(self.done(first['execution']['id'])['execution']['status'],'succeeded')
        self.assertEqual(self.done(second['execution']['id'])['stdout'],'queued')
        result=self.call(process_id=pid,command='pwd; printf "%s:%s:%s:" "$value" "$SHARED" "$trace"; fn; printf warning >&2; false')
        self.assertEqual(result['stdout'],'/tmp/work\n42:yes:AB:function',result)
        self.assertEqual(result['stderr'],'warning');self.assertEqual(result['exit_code'],1)
        self.assertEqual(self.call(command='printf %s "${value-unset}"')['stdout'],'unset')
        self.assertEqual(self.call(process_id=other,command='printf %s "${value-unset}"')['stdout'],'unset')
        self.assertIn('error',self.call(process_id=pid,command='true',cwd='/tmp'))

    def test_cancel_running_and_queued_commands_preserves_local_state(self):
        pid=self.create();self.call(process_id=pid,command='kept=42')
        active=self.call(process_id=pid,command='printf ready; sleep 30',mode='async')
        queued=self.call(process_id=pid,command='should_not_run=yes',mode='async')
        until=time.monotonic()+3
        while 'ready' not in self.call('bash_execution',action='read',execution_id=active['execution']['id'])['stdout']:
            self.assertLess(time.monotonic(),until);time.sleep(.02)
        cancelled=self.call('bash_execution',action='cancel',execution_id=queued['execution']['id'])
        self.assertEqual(cancelled['execution']['status'],'cancelled',cancelled)
        self.call('bash_execution',action='cancel',execution_id=active['execution']['id'])
        self.assertEqual(self.done(active['execution']['id'])['execution']['status'],'cancelled')
        self.assertEqual(self.call(process_id=pid,command='printf "%s:%s" "$kept" "${should_not_run-unset}"')['stdout'],'42:unset')
        timed=self.call(process_id=pid,command='while :; do :; done',timeout=.05)
        self.assertTrue(timed['timed_out'],timed)
        self.assertEqual(self.call(process_id=pid,command='printf %s "$kept"')['stdout'],'42')

    def test_delete_and_resume_never_restore_or_reuse_process(self):
        pid=self.create();self.call(process_id=pid,command='kept=42')
        task=self.call(process_id=pid,command='sleep 30',mode='async')
        self.call('bash_process',action='delete',process_id=pid)
        self.assertEqual(self.done(task['execution']['id'])['execution']['status'],'cancelled')
        self.assertIn('error',self.call(process_id=pid,command='touch BAD'))
        fresh=self.create();self.assertNotEqual(fresh,pid)
        self.assertEqual(self.call(process_id=fresh,command='printf %s "${kept-unset}"')['stdout'],'unset')
        owner=self.sandbox.execution_owner;self.sandbox.close()
        with patch.dict(os.environ,{'PYTHONUSERBASE':str(self.root/'empty-site')}):
            self.sandbox=Sandbox(self.root,network=False,gpu=False,execution_owner=owner,non_builtin_tools=definitions())
        self.addCleanup(self.sandbox.close)
        records=self.call('bash_process',action='list')['processes']
        self.assertTrue(all(record['status']=='closed' for record in records))
        self.assertIn('error',self.call(process_id=fresh,command='touch BAD'))
        self.assertFalse((self.root/'BAD').exists())

    def test_real_ssh_reuses_connection_and_remote_state_and_never_reconnects_implicitly(self):
        from ssh_support import local_ssh
        self.sandbox.close()
        with local_ssh(self.root/'ssh') as fixture:
            with patch.dict(os.environ,{'PYTHONUSERBASE':str(self.root/'empty-site')}):
                self.sandbox=Sandbox(self.root,network=True,gpu=False,timeout=5,non_builtin_tools=definitions())
            try:
                prefix=['/usr/bin/ssh','-T','-p',str(fixture.port),'-i','/workspace/ssh/client-key',
                        '-o','IdentitiesOnly=yes','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
                        '-o','UserKnownHostsFile=/workspace/ssh/known_hosts',fixture.user+'@127.0.0.1']
                pid=self.create(name='remote shell',ssh_argv=prefix)
                first=self.call(process_id=pid,command='value=41; export REMOTE=yes; cd nested; fn(){ printf remote-function; }; trace=; printf initialized')
                task=self.call(process_id=pid,command='sleep .1; value=$((value+1)); trace=A; printf async-result',mode='async')
                next_task=self.call(process_id=pid,command='trace+=B; printf second',mode='async')
                self.assertEqual(self.done(task['execution']['id'])['stdout'],'async-result')
                self.assertEqual(self.done(next_task['execution']['id'])['stdout'],'second')
                result=self.call(process_id=pid,command='pwd; printf "%s:%s:%s:" "$value" "$REMOTE" "$trace"; fn; printf remote-warning >&2; false')
                self.assertEqual(result['stdout'],str(fixture.remote/'nested')+'\n42:yes:AB:remote-function',result)
                self.assertEqual(result['stderr'],'remote-warning');self.assertEqual(result['exit_code'],1)
                self.assertEqual(first['execution']['pid'],result['execution']['pid'])
                failed=self.call(process_id=pid,command='printf before-exit; exit 7')
                self.assertTrue(failed['execution']['memory_lost'],failed)
                self.assertIn('error',self.call(process_id=pid,command='touch BAD'))
                self.assertEqual(fixture.log.read_text().count('Accepted publickey'),1)
                self.assertFalse((fixture.remote/'nested'/'BAD').exists())
            finally:self.sandbox.close()

if __name__=='__main__':unittest.main()
