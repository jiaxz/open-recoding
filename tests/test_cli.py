"""CLI commands use retained test Sessions and synthetic/local models only."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

from recoding_cli.control import request
from workspace_support import temporary_workspace

PROJECT = Path(__file__).resolve().parents[1]


class CLITests(unittest.TestCase):
    def setUp(self):
        self.root = Path(temporary_workspace(prefix='cli-').name)
        self.env = {key: value for key, value in os.environ.items() if not key.startswith('GEMINI_')}
        self.env['PYTHONDONTWRITEBYTECODE'] = '1'
        self.folder = None

    def cli(self, *args, check=True, input=None):
        result = subprocess.run([sys.executable, '-m', 'recoding_cli', *map(str, args)],
            cwd=PROJECT, env=self.env, input=input, text=True, capture_output=True, timeout=40)
        if check: self.assertEqual(result.returncode, 0, result.stderr+result.stdout)
        return result

    def tearDown(self):
        if self.folder:
            request(self.folder, 'shutdown')
            end = time.monotonic()+12
            while request(self.folder, 'status') is not None and time.monotonic() < end: time.sleep(.05)

    def wait(self, predicate):
        deadline = time.monotonic()+15
        while True:
            result = predicate()
            if result: return result
            self.assertLess(time.monotonic(), deadline, str(self.root))
            time.sleep(.1)

    def create(self):
        from session import SessionStore
        store=SessionStore.create(self.root,'中文 CLI',{})
        self.folder=store.folder
        return self.folder

    def test_chat_without_a_message_does_not_create_or_read_model_configuration(self):
        unread=self.root/'unused.env'
        unread.write_text('GEMINI_API_KEY="invalid unterminated value\n')
        result=self.cli('chat','-C',self.root,'--env-file',unread,input='/help\n\n@master\n/quit\n')
        self.assertIn('/resume',result.stdout)
        self.assertFalse((self.root/'.recoding').exists())
        self.assertNotIn('Error:',result.stderr)

    def test_cli_has_no_offline_browse_or_offline_mutation(self):
        folder=self.create()
        from session import SessionStore
        original=(folder/'session.json').read_bytes()
        for args in [('status',),('messages',),('send','-p','must not write'),('config','max_subagents=4'),('rename','must not change')]:
            self.assertNotEqual(self.cli(*args,'-s',folder,check=False).returncode,0)
        self.assertEqual((folder/'session.json').read_bytes(),original)
        listed=json.loads(self.cli('list','-C',self.root,'--json').stdout)
        self.assertEqual([s['id'] for s in listed],[SessionStore(folder).id])

    def test_detached_control_stop_release_resume_and_image(self):
        folder=self.create()
        self.cli('resume','-s',folder,'--demo','--no-network','--no-gpu','--detach','--json')
        self.wait(lambda:request(folder,'status').get('master_status')=='sleeping')
        self.cli('send','-s',folder,'--to','master','-p','检查目录','--image',PROJECT/'tests/fixtures/small.jpg')
        self.wait(lambda:any(m['kind']=='human_output' for m in request(folder,'messages')['messages']))
        self.assertIn('Demo 执行完成',self.cli('messages','-s',folder).stdout)
        self.cli('config','-s',folder,'--max-agents','3','--idle-sleep','2')
        self.assertEqual(request(folder,'status')['settings']['max_subagents'],3)
        self.cli('stop','-s',folder)
        self.wait(lambda:request(folder,'status') is None)
        self.assertNotEqual(self.cli('send','-s',folder,'-p','offline rejected',check=False).returncode,0)
        self.cli('resume','-s',folder,'--demo','--no-network','--no-gpu','--detach','--json')
        self.wait(lambda:request(folder,'status').get('master_status')=='sleeping')
        self.cli('close','-s',folder)
        self.assertIsNone(request(folder,'status'))
        from session import SessionHostProtocol
        self.assertTrue(SessionHostProtocol.available(folder))

    def test_foreground_exit_is_explicit_and_message_only(self):
        result = self.cli('run', '-C', self.root, '--demo', '--no-network', '--no-gpu',
            '--env-file', self.root/'absent.env', '-p', 'Inspect workspace', '--max-messages', '1', '--timeout', '15', '--format', 'jsonl')
        records = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertTrue(any(item['kind']=='human_output' for item in records))
        self.assertTrue(all(item['kind'] in {'human_input','human_output','peer'} for item in records))
        from recoding_cli.catalog import discover
        self.folder = Path(discover(self.root)[0]['session_directory'])
        self.assertIsNone(request(self.folder, 'status'))

    def test_session_navigation_stops_previous_host(self):
        from session import SessionStore,SessionHostProtocol
        from recoding_cli.sessions import CliSessionNavigator
        one=SessionStore.create(self.root/'one','first',{})
        two=SessionStore.create(self.root/'two','second',{})
        navigation=CliSessionNavigator(self.root,lambda _: {'demo':True,'network':False,'gpu':False})
        try:
            first=navigation.restore(str(one.folder))
            self.wait(lambda:first.call('status').get('master_status')=='sleeping')
            second=navigation.restore(str(two.folder))
            self.wait(lambda:second.call('status').get('master_status')=='sleeping')
            self.assertIsNone(request(one.folder,'status'))
            self.assertTrue(SessionHostProtocol.available(one.folder))
            current_pid=navigation.current_host.session.master.process.pid
            navigation.restore(str(two.folder))
            self.assertEqual(navigation.current_host.session.master.process.pid,current_pid)
        finally:navigation.close()
        self.assertTrue(SessionHostProtocol.available(two.folder))

    def test_interactive_terminal_unicode_and_message_only_output(self):
        import pty, select
        master, slave = pty.openpty()
        import fcntl,struct,termios
        fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',32,100,0,0))
        env = {**self.env, 'TERM':'xterm-256color', 'PROMPT_TOOLKIT_NO_CPR':'1'}
        from session import SessionStore
        other=SessionStore.create(self.root/'secondary','tui-secondary',{'model':'gemini-4-argon'})
        process = subprocess.Popen([sys.executable, '-m', 'recoding_cli', 'chat', '-C', str(self.root), '--name','tui-primary',
            '--demo', '--no-network', '--no-gpu', '--color', 'always', '--env-file', str(self.root/'absent.env')],
            cwd=PROJECT, env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        data = bytearray()
        def read_until(marker, after=0):
            deadline=time.monotonic()+15
            while marker.encode() not in data[after:]:
                self.assertIsNone(process.poll(), data.decode(errors='replace'))
                self.assertLess(time.monotonic(),deadline,data.decode(errors='replace'))
                if select.select([master],[],[],.1)[0]: data.extend(os.read(master,65536))
        try:
            read_until('发给')
            self.assertIn(b'\x1b[?1049h', data)  # Actual full-screen terminal mode.
            self.assertFalse((self.root/'.recoding').exists(),'Opening the TUI must not create a Session')
            os.write(master,b'/help\r')
            read_until('帮助')
            os.write(master,b'\x1b')
            time.sleep(.1)
            self.assertFalse((self.root/'.recoding').exists(),'Help and Esc must not create a Session')
            os.write(master, '你好，请检查目录\r'.encode())
            read_until('Demo 执行完成')
            from recoding_cli.catalog import discover
            first=next(item for item in discover(self.root) if item['name']=='tui-primary')
            position=len(data)
            os.write(master,('/resume '+str(other.folder)+'\r').encode())
            read_until('\x1b[?1049h',after=position)
            frame=data.index(b'\x1b[?1049h',position)
            read_until('tui-secondary',after=frame)
            self.wait(lambda:request(other.folder,'status') is not None)
            self.assertIsNone(request(first['session_directory'],'status'))
            self.wait(lambda:request(other.folder,'status').get('master_status')=='sleeping')
            os.write(master,b'/wake\r')
            self.wait(lambda:any('/wake' in item['content'] for item in request(other.folder,'messages')['messages'] if item['kind']=='human_input'))
            position=len(data)
            os.write(master,('/resume '+first['session_directory']+'\r').encode())
            read_until('\x1b[?1049h',after=position)
            frame=data.index(b'\x1b[?1049h',position)
            read_until('tui-primary',after=frame)
            os.write(master, b'\x04')  # Same graceful exit as /quit, including all owned Hosts.
            process.wait(timeout=15)
            self.assertEqual(process.returncode,0)
            self.assertNotIn(b'Model call #',data)
            self.assertNotIn(b'tool_start',data)
            self.assertNotIn(b'?[32m',data)
            self.assertNotIn(b'?[36m',data)
            from recoding_cli.catalog import discover
            self.folder=Path(first['session_directory'])
            self.assertIsNone(request(self.folder,'status'))
            self.assertIsNone(request(other.folder,'status'))
            (self.root/'terminal.log').write_bytes(data)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
            os.close(master)

    def test_local_api_python_task_with_finite_shell_driver(self):
        from model_support import local_model
        stage = 'waiting'
        def respond(body, number):
            nonlocal stage
            def call(name, fields):
                return {'role':'assistant', 'tool_calls':[{'index':0, 'id':str(number), 'type':'function',
                    'function':{'name':name, 'arguments':json.dumps(fields)}}]}
            if stage == 'waiting' and any('CLI_SUM_TASK' in str(m.get('content','')) for m in body['messages'] if m['role']=='user'):
                stage='executing'
                return call('bash', {'command':'/usr/bin/python3 -c '+__import__('shlex').quote("from pathlib import Path\ntry:\n    private_text = Path('model.env').read_text()\nexcept PermissionError:\n    private_text = ''\nassert not private_text\nvalue=sum(range(1,101))\nPath('sum.txt').write_text(str(value))\nprint(value)")})
            if stage == 'executing':
                result=json.loads(next(m['content'] for m in reversed(body['messages']) if m['role']=='tool'))
                self.assertEqual(result['stdout'].strip(),'5050')
                stage='reported'
                return call('send_user_message', {'message':'CLI_SUM_DONE **5050**'})
            return call('sleep', {'seconds':300})
        with local_model(self.root/'model', respond) as model:
            self.env.update(GEMINI_API_KEY='local-test-only', SSL_CERT_FILE=str(model.cert),
                            NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost')
            (self.root/'workspace').mkdir()
            env_file=self.root/'workspace/model.env'
            env_file.write_text('GEMINI_BASE_URL='+model.url+'\nGEMINI_MODEL=ignored-for-new-session\n')
            result=self.cli('run', '-C', self.root/'workspace', '--history','0',
                '--env-file', env_file, '--no-network','--no-gpu','--api-timeout','5',
                '-p','CLI_SUM_TASK', '--until','CLI_SUM_DONE','--timeout','15','--format','jsonl')
            self.assertIn('CLI_SUM_DONE',result.stdout)
            self.assertEqual((self.root/'workspace/sum.txt').read_text(),'5050')
            self.assertEqual(model.errors,[])
            self.assertTrue(all(item['model']=='grok-4.7' for item in model.requests))
            (self.root/'test-report.json').write_text(json.dumps({'passed':True,'model_calls':len(model.requests),
                'sum':5050,'real_api':False,'workspace':str(self.root/'workspace')},indent=2))

    def test_current_team_messages_and_processes_through_cli_host(self):
        from unittest.mock import patch
        from model_support import local_model
        from recoding_cli.client import Client
        from recoding_cli.host import CliSessionHost
        from recoding_cli.tui import TerminalUI
        from recoding_cli.render import Renderer
        from prompt_toolkit.input import DummyInput
        from prompt_toolkit.output import DummyOutput
        from types import SimpleNamespace
        from session import SessionStore,SessionHostProtocol
        stages={'master':0,'sub':0}
        def respond(body,number):
            master=any(tool['function']['name']=='create_agent' for tool in body['tools'])
            text=' '.join(str(item.get('content','')) for item in body['messages'] if item['role']=='user')
            calls=[]
            if master and stages['master']==0 and 'CLI_TEAM_TASK' in text:
                stages['master']=1;calls=[('create_agent',{'name':'Analyst','icon':'🔬'})]
            elif master and stages['master']==1:
                created=json.loads(next(item['content'] for item in reversed(body['messages']) if item['role']=='tool'))
                stages['master']=2;calls=[('send_agent_message',{'agent_id':created['agent_id'],'message':'CLI_PEER_TASK: check the evidence'}),('sleep',{'seconds':300})]
            elif master and stages['master']==2 and 'CLI_SUB_ACK' in text:
                stages['master']=3;calls=[('send_user_message',{'message':'CLI_MASTER_REPORT **verified**'}),('sleep',{'seconds':300})]
            elif not master and stages['sub']==0 and 'CLI_PEER_TASK' in text:
                stages['sub']=1;calls=[('send_user_message',{'message':'CLI_SUB_REPORT **evidence**'}),
                    ('send_agent_message',{'agent_id':'master','message':'CLI_SUB_ACK: evidence checked'}),('sleep',{'seconds':300})]
            else:calls=[('sleep',{'seconds':300})]
            return {'role':'assistant','content':'Internal step note; not a human message',
                'tool_calls':[{'index':index,'id':f'{number}-{index}','type':'function','function':{'name':name,
                    'arguments':json.dumps({**fields,'_step_note':'Local fixture step'})}} for index,(name,fields) in enumerate(calls)]}
        with local_model(self.root/'model',respond) as model, patch.dict(os.environ,{
            'RECODING_API_KEY':'local-test-only','SSL_CERT_FILE':str(model.cert),
            'NO_PROXY':'127.0.0.1,localhost','no_proxy':'127.0.0.1,localhost'}):
            config={'base_url':model.url,'demo':False,'network':False,'gpu':False,'api_timeout':5,'tool_timeout':5}
            store=SessionStore.create(self.root/'workspace','CLI team',config)
            self.folder=store.folder
            host=CliSessionHost(store.folder,config)
            try:
                client=Client(store.folder);client.send('CLI_TEAM_TASK: delegate and report')
                self.wait(lambda:len([row for row in client.call('messages')['messages'] if row['kind']=='human_output'])==2)
                feed=client.call('messages')['messages'];metadata=client.call('agents')
                self.assertEqual(len(metadata),2)
                self.assertEqual(len([row for row in feed if row['kind']=='peer']),2)
                self.assertFalse(any('Internal step note' in row['content'] for row in feed))
                status=client.call('status')
                self.assertEqual(status['agent_token_usage']['master']['input_tokens'],100)
                ui=TerminalUI(client,Renderer(),SimpleNamespace(),input=DummyInput(),output=DummyOutput())
                try:
                    ui.agents=metadata;ui.status=status
                    self.assertIn('Input 100',next(row[1] for row in ui.agent_options() if row[3]=='master'))
                finally:ui.close()
                resource=host.session._resource_request
                resource('master','python_process',{'action':'create','name':'analysis memory'})
                process=resource('master','bash_process',{'action':'create','name':'Bash compute'})['process']
                resource('master','bash',{'process_id':process['id'],'command':'sleep 30','mode':'async'})
                def processes():
                    rows=json.loads(self.cli('processes','-s',store.folder,'--json').stdout)
                    return rows if len(rows)==2 and any(row['phase']=='run' for row in rows) else None
                rows=self.wait(processes)
                self.assertEqual({row['kind'] for row in rows},{'bash','python'})
                bash=next(row for row in rows if row['kind']=='bash')
                self.assertEqual(bash['execution']['mode'],'async')
                self.assertGreaterEqual(bash['execution']['started_at'],bash['process']['created_at'])
                index=json.loads((store.folder/'agents/index.json').read_text())
                self.assertFalse({'human_outbox','deferred_system','master_runs'} & index.keys())
                self.assertTrue(index['master']['runs']);self.assertEqual(model.errors,[])
            finally:host.close()
            self.assertTrue(SessionHostProtocol.available(store.folder))



if __name__ == '__main__': unittest.main()
