"""Session is callable as an aggregate without importing or starting Web."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

from workspace_support import temporary_workspace


PROGRAM = r'''
import sys, time
from pathlib import Path
from session import Session
root, picture = Path(sys.argv[1]), Path(sys.argv[2]).read_bytes()
assert not any(name.startswith('recoding_web') for name in sys.modules)
config = {'demo':True, 'network':False, 'gpu':False, 'tool_timeout':5}
session = Session.create(root, 'shell-independent', config=config)
def wait(test):
    deadline=time.monotonic()+12
    while not test():
        assert session.master.status != 'failed', session.snapshot().get('error')
        assert time.monotonic()<deadline, session.snapshot(events=False)
        time.sleep(.02)
try:
    wait(lambda: session.master.status=='sleeping')
    same_id, saved = session.id, session.store.folder
    child=session.team.dispatch('master','create_agent',{'name':'Independent member'})['agent_id']
    wait(lambda: session.agent_snapshot(child)['status']=='sleeping')
    source = session.agent_store(child)[0].load_state()
    assert not any(m.get('_user_input') for m in source['messages'])
    uploaded=session.upload(picture,'input.jpg',agent_id=child)
    receipt=session.submit('Inspect this workspace', [uploaded['id']], agent_id=child)
    wait(lambda: session.agent_snapshot(child)['status']=='sleeping' and any(e['type']=='human_message' for e in session.agent_snapshot(child)['events']))
    child_state = session.agent_store(child)[0].load_state()
    assert any(m.get('_agent_message_id')==receipt['queued'] for m in child_state['messages'])
    assert session.master.store.load_state()['messages'][0]['role']=='system'
    session.configure('set_limit', 5)
    session.configure('set_idle_sleep', 20)
    assert session.snapshot()['max_subagents']==5
    session.cancel()
    wait(lambda: session.master.status=='stopped' and all(a['status']=='stopped' for a in session.team.listing()))
    receipt=session.submit('Keep unread until Master resumes this member', agent_id=child)
    assert session.agent_snapshot(child)['status']=='stopped'
finally:
    session.close()
session=Session.resume(saved,config=config)
try:
    wait(lambda: session.master.status=='sleeping')
    assert session.id==same_id
    assert session.snapshot()['max_subagents']==5
    assert session.snapshot()['idle_sleep_seconds']==20
    assert set(session.team.agents) == {'master'}
    assert session.agent_snapshot(child)['status']=='stopped'
    assert any(m.get('_agent_message_id')==receipt['queued'] for m in session.team.pending(child,[]))
finally:
    session.close()
assert not any(name.startswith('recoding_web') for name in sys.modules)
print('Session API: create, named member, upload, directed input, settings, Stop All and Master-only Resume passed')
'''


TASK_PROGRAM = r'''
import json, os, sys, time
from pathlib import Path
from multiagent import Agent
from multiagent import MultiAgentSystem
from session import Session
from model_support import local_model

root = Path(sys.argv[1]); workspace = root / 'workspace'
workspace.mkdir()
stage = 'waiting'

def respond(request, number):
    global stage
    def call(name, arguments):
        return {'role':'assistant', 'tool_calls':[{'index':0, 'id':f'task-{number}', 'type':'function',
                'function':{'name':name, 'arguments':json.dumps(arguments)}}]}
    task_arrived = any(m.get('role')=='user' and 'SUM_1_TO_100_TASK' in str(m.get('content'))
                       for m in request['messages'])
    if task_arrived and stage == 'waiting':
        stage = 'executing'
        return call('bash', {'command':'/usr/bin/python3 -c '+__import__('shlex').quote("from pathlib import Path\nimport json\nvalue = sum(range(1, 101))\nPath('result.json').write_text(json.dumps({'sum': value, 'count': 100}))\nprint(value)")})
    if stage == 'executing':
        tools = [m for m in request['messages'] if m['role']=='tool']
        result = json.loads(tools[-1]['content'])
        assert result.get('exit_code') == 0 and result.get('stdout', '').strip() == '5050', result
        stage = 'reported'
        return call('send_user_message', {'message':'SUM_TASK_DONE: 5050; result.json 已保存。'})
    return call('sleep', {'seconds':300})

with local_model(root / 'model', respond) as model:
    os.environ['SSL_CERT_FILE'] = str(model.cert)
    os.environ['GEMINI_API_KEY'] = 'local-test-only'
    os.environ['NO_PROXY'] = '127.0.0.1,localhost'
    os.environ['no_proxy'] = '127.0.0.1,localhost'
    config = {'base_url':model.url, 'model':'grok-4.7', 'network':False, 'gpu':False,
              'tool_timeout':5, 'api_timeout':5}
    session = Session.create(workspace, 'python-task', config=config)
    process = session.master.process
    def wait(predicate):
        deadline = time.monotonic() + 15
        while not predicate():
            assert session.master.status != 'failed', session.snapshot(events=False).get('error')
            assert time.monotonic() < deadline, 'Session task timed out'
            with session.condition:
                session.condition.wait(.02)
    try:
        assert isinstance(session.master, Agent)
        assert isinstance(session.team, MultiAgentSystem)
        assert not any(name.startswith('recoding_web') for name in sys.modules)
        wait(lambda: session.master.status == 'sleeping')
        receipt = session.submit('SUM_1_TO_100_TASK: 用 Python 计算 1 到 100 的和，保存到 result.json，完成后发消息给我。')
        def completed():
            return session.master.status == 'sleeping' and any(
                m['kind']=='human_output' and 'SUM_TASK_DONE' in m['content']
                for m in session.messages()['messages'])
        wait(completed)
        assert json.loads((workspace / 'result.json').read_text()) == {'sum':5050, 'count':100}
        assert any(m.get('_agent_message_id')==receipt['queued']
                   for m in session.store.load_state()['messages'])
        assert any(e['type']=='tool_end' and e['name']=='bash' and e['result']['stdout'].strip()=='5050'
                   for e in session.events)
        # A task report does not end an endless Agent. The caller explicitly stops it.
        assert session.master.status == 'sleeping'
        session.cancel()
        wait(lambda: session.master.status == 'stopped')
        assert model.errors == [], model.errors
        assert not any(name.startswith('recoding_web') for name in sys.modules)
        report = {'sum':5050, 'model_calls':len(model.requests), 'workspace':str(workspace),
                  'session_directory':str(session.store.folder), 'web_imported':False}
    finally:
        session.close()
    assert process.poll() is not None, 'Session.close must release its worker'
print(json.dumps(report, ensure_ascii=False))
'''


class SessionAPITests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("bwrap") and shutil.which("openssl"), "requires Bubblewrap and OpenSSL")
    def test_python_program_completes_task_via_session(self):
        project = Path(__file__).resolve().parents[1]
        with temporary_workspace(prefix="session-task-") as root:
            environment = {key: value for key, value in os.environ.items() if not key.startswith("GEMINI_")}
            result = subprocess.run([sys.executable, "-B", "-c", TASK_PROGRAM, root], cwd=project,
                                    env=environment, capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            report = json.loads(result.stdout)
            self.assertEqual(report['sum'], 5050)
            self.assertFalse(report['web_imported'])
            self.assertTrue(Path(report['workspace']).is_relative_to(project / 'sessions/tests'))
            self.assertTrue(Path(report['session_directory']).is_relative_to(Path(root)))
            Path(root, 'test-report.json').write_text(json.dumps(
                {**report, 'passed': True, 'test_directory': root}, ensure_ascii=False, indent=2) + '\n')

    @unittest.skipUnless(shutil.which("bwrap"), "requires the production Linux sandbox")
    def test_session_aggregate_without_web(self):
        project = Path(__file__).resolve().parents[1]
        with temporary_workspace(prefix="recoding-session-api-") as root:
            environment = {key: value for key, value in os.environ.items() if not key.startswith("GEMINI_")}
            result = subprocess.run([sys.executable, "-B", "-c", PROGRAM, root,
                                     str(project / "tests/fixtures/small.jpg")], cwd=project,
                                    env=environment, capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stderr+result.stdout)
            self.assertIn("Session API:", result.stdout)


if __name__ == "__main__":
    unittest.main()
