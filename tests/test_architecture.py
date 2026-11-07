"""Enforce core layers and independent application shells."""

import ast
import importlib.util
from contextlib import AbstractContextManager
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import unittest

from multiagent.loop import AgentLoop as Agent


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = {'recoding_cli', 'recoding_web', 'argparse', 'readline', 'termios', 'fcntl', 'ctypes'}
ADAPTERS = {'multiagent.system', 'session', 'multiagent.agent',
            'multiagent.client', 'multiagent.client', 'node.backends.sandbox',
            'multiagent.storage', 'multiagent.worker'}


HEADLESS_PROGRAM = r'''
import importlib.abc, json, sys
forbidden = set(json.loads(sys.argv[1]))
class DenyAdapters(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name+'.') for name in forbidden):
            raise AssertionError('Core imported forbidden module: '+fullname)
sys.meta_path.insert(0,DenyAdapters())
class NoTerminal:
    def __getattr__(self,name):
        raise AssertionError('Headless core accessed terminal IO: '+name)
saved = sys.stdin,sys.stdout,sys.stderr
try:
    sys.stdin = sys.stdout = sys.stderr = NoTerminal()
    from multiagent import AgentLoop as Agent
    class Model:
        calls=0
        def complete(self,messages):
            self.calls+=1
            assert messages[0]=={'role':'system','content':'A headless agent with an echo tool.'}
            if self.calls==1:
                return {'role':'assistant','tool_calls':[{'id':'one','type':'function',
                    'function':{'name':'echo','arguments':'{"text":"42"}'}}]}
            if self.calls == 3: raise KeyboardInterrupt()
            assert messages[-1]['role']=='tool'
            assert json.loads(messages[-1]['content'])['stdout']=='42'
            return {'role':'assistant','content':'42'}
    class Executor:
        calls=[]
        def execute(self,name,arguments):
            self.calls.append(name)
            return {'stdout':json.loads(arguments).get('text',''),'exit_code':0}
    class MemoryHistory:
        records=[]
        def record(self,kind,**fields): self.records.append(kind)
    model,executor,history=Model(),Executor(),MemoryHistory()
    events=[]
    agent=Agent(model,executor,system='A headless agent with an echo tool.',
                on_event=events.append,history=history)
    try: agent.run()
    except KeyboardInterrupt: pass
    result=next(e['content'] for e in events if e['type']=='model_end' and e.get('content'))
    assert not forbidden.intersection(sys.modules)
    report={'answer':result,'model_calls':model.calls,'tools':executor.calls,
            'events':[e['type'] for e in events],'history':history.records}
finally:
    sys.stdin,sys.stdout,sys.stderr=saved
print(json.dumps(report))
'''


class FixedModel:
    def __init__(self):
        self.last_usage = {'prompt_tokens': 10, 'completion_tokens': 1, 'total_tokens': 11}
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        if self.calls == 1:
            return {'role': 'assistant', 'tool_calls': [{'id': 'one', 'type': 'function',
                'function': {'name': 'echo', 'arguments': '{"text":"42"}'}}]}
        if self.calls == 3: raise KeyboardInterrupt()
        return {'role': 'assistant', 'content': '42'}


class EchoExecutor:
    def __init__(self):
        self.arguments = []

    def execute(self, name, arguments):
        self.arguments.append(arguments)
        return {'stdout': json.loads(arguments).get('text',''), 'exit_code': 0}


class ArchitectureTests(unittest.TestCase):
    def test_core_import_graph_never_depends_on_frontend_or_concrete_adapters(self):
        pending, seen = ['multiagent.loop'], set()
        while pending:
            module = pending.pop()
            if module in seen:
                continue
            seen.add(module)
            location = ROOT / 'core' / module.replace('.', '/')
            path = location / '__init__.py' if location.is_dir() else location.with_suffix('.py')
            package = module if location.is_dir() else module.rpartition('.')[0]
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [name.name for name in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [importlib.util.resolve_name('.' * node.level + (node.module or ''), package)
                             if node.level else node.module]
                for name in names:
                    self.assertFalse(any(name == prefix or name.startswith(prefix + '.')
                                         for prefix in FRONTEND | ADAPTERS), (module, name))
                    if name.split('.')[0] == 'multiagent':
                        pending.append(name)

    def test_layers_and_apps_have_no_reverse_imports(self):
        core = ROOT / 'core'
        layers = {
            'multiagent': {'session', 'tools', 'node', 'recoding_web', 'recoding_cli'},
            'session': {'recoding_web', 'recoding_cli'},
            'tools': {'session','multiagent.system','recoding_web','recoding_cli'},
            'node': {'agent','session','multiagent','tools','recoding_web','recoding_cli'},
        }
        self.assertEqual({p.name for p in core.iterdir() if p.is_dir() and (p/'__init__.py').is_file()}, set(layers))
        roots = [(core/layer, core, forbidden) for layer, forbidden in layers.items()]
        roots += [(ROOT/'apps'/'web'/'recoding_web', ROOT/'apps'/'web', {'recoding_cli'}),
                  (ROOT/'apps'/'cli'/'recoding_cli', ROOT/'apps'/'cli', {'recoding_web'})]
        for folder, source, forbidden in roots:
            for path in folder.rglob('*.py'):
                module = '.'.join(path.relative_to(source).with_suffix('').parts)
                package = module.rpartition('.')[0]
                for node in ast.walk(ast.parse(path.read_text())):
                    names = []
                    if isinstance(node, ast.Import): names = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        names = [importlib.util.resolve_name('.'*node.level+(node.module or ''), package)
                                 if node.level else node.module]
                    for name in names:
                        self.assertFalse(any(name == prefix or name.startswith(prefix+'.') for prefix in forbidden), (str(path), name))

    def test_both_app_entry_points(self):
        self.assertFalse((ROOT/'recoding').exists())
        self.assertFalse((ROOT/'core'/'recoding').exists())
        self.assertFalse((ROOT/'core'/'__init__.py').exists())
        metadata = (ROOT/'pyproject.toml').read_text()
        self.assertIn('recoding = "recoding_cli.main:main"', metadata)
        self.assertIn('recoding-web = "recoding_web.server:main"', metadata)
        for module, option in [('recoding_cli', '--session'), ('recoding_web', '--port')]:
            program = subprocess.run([sys.executable, '-m', module, '--help'], cwd=ROOT,
                                     capture_output=True, text=True, timeout=10)
            self.assertEqual(program.returncode, 0, program.stderr)
            self.assertIn(option, program.stdout)

    def test_core_runs_in_fresh_process_with_terminal_and_adapters_forbidden(self):
        child = subprocess.run([sys.executable, '-c', HEADLESS_PROGRAM,
                                json.dumps(sorted(FRONTEND | ADAPTERS))],
                               cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(child.returncode, 0, child.stderr)
        result = json.loads(child.stdout)
        self.assertEqual(result['answer'], '42')
        self.assertEqual(result['model_calls'], 3)
        self.assertEqual(result['tools'], ['echo', 'sleep'])
        self.assertIn('tool_end', result['events'])
        self.assertEqual(result['history'][-1], 'loop_end')

    def test_subscribers_cannot_mutate_execution_or_token_usage(self):
        client, executor = FixedModel(), EchoExecutor()
        def subscriber(event):
            if event['type'] == 'model_end':
                event['usage']['prompt_tokens'] = 999
                for call in event['calls']:
                    call['function']['arguments'] = '{"text":"corrupted"}'
        agent = Agent(client, executor, on_event=subscriber)
        with self.assertRaises(KeyboardInterrupt):
            agent.run()
        self.assertEqual(executor.arguments[0], '{"text":"42"}')
        self.assertEqual(client.last_usage['prompt_tokens'], 10)

    def test_history_sink_receives_copies_of_core_state(self):
        class MutatingHistory:
            def record(self, kind, **fields):
                if kind == 'message':
                    fields['message']['content'] = 'corrupted'
                if isinstance(fields.get('messages'), list):
                    fields['messages'].clear()
        client, executor = FixedModel(), EchoExecutor()
        agent = Agent(client, executor, system='custom system', history=MutatingHistory())
        with self.assertRaises(KeyboardInterrupt):
            agent.run()
        self.assertEqual(agent.messages[0]['content'], 'custom system')
        self.assertIn('42', [m.get('content') for m in agent.messages])
        self.assertEqual(json.loads(agent.messages[2]['content'])['stdout'], '42')

    def test_scope_exit_interruption_keeps_committed_work(self):
        class CancelOnExit(AbstractContextManager):
            def __exit__(self, *args):
                raise KeyboardInterrupt()
        agent = Agent(FixedModel(), EchoExecutor())
        with self.assertRaises(KeyboardInterrupt):
            agent.run(cancel_scope=CancelOnExit())
        record = json.loads(agent.messages[-1]['content'].split('\n', 1)[1])
        self.assertEqual(record['reason'], 'cancelled')
        self.assertEqual(record['location']['phase'], 'model')
        retained = deepcopy(agent.messages)
        restored = Agent(FixedModel(), EchoExecutor())
        restored.restore(retained, iteration=agent.model_step, tool_count=agent.tool_count)
        self.assertEqual(restored.messages, retained)


if __name__ == '__main__':
    unittest.main()
