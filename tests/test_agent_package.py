"""The runtime package and its loop work without Session, Nodes or Apps."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from workspace_support import temporary_workspace


PROGRAM = r'''
import importlib.abc, json, sys, time
from pathlib import Path
class NoOuterLayers(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'agent','multiagent','session','node','recoding','recoding_web','recoding_cli','tools'}:
            raise AssertionError('Copied runtime imported an outer package: '+fullname)
sys.meta_path.insert(0, NoOuterLayers())
sys.path.insert(0, sys.argv[1])
from independent_agent import MultiAgentSystem, AgentStore
from independent_agent.config import configuration
from independent_agent.tools import tool
from independent_agent.storage import atomic_json
root = Path(sys.argv[2]) / 'workspace'
root.mkdir()
schema = tool('bash','Fixture executor',{'command':{'type':'string'}},('command',))
config = configuration({'demo':True,'network':False,'gpu':False,'tool_timeout':5,
                        'non_builtin_tools':[{'schema':schema}]})
state = root / '.recoding' / 'team-state'
store = AgentStore.create(state,workspace=root,name='Independent runtime',settings=config)
store.metadata.update(format='standalone.team',version=1)
atomic_json(state/'agent.json',store.metadata)
def resource_request(source,action,fields,**options):
    assert action == 'bash', action
    return {'stdout':'external tool result','exit_code':0}
def open_team():
    return MultiAgentSystem(AgentStore(state,workspace=root),dict(config),resource_request=resource_request)
team = open_team()
agent = team.master
def wait(test):
    until = time.monotonic()+12
    with agent.condition:
        while not test():
            assert agent.status != 'failed', agent.error
            assert time.monotonic() < until, (agent.status, agent.error)
            agent.condition.wait(.02)
try:
    assert agent.team is team and agent.process is None
    team.start()
    wait(lambda: agent.status == 'sleeping')
    queued = agent.submit('Inspect this workspace')
    wait(lambda: agent.status == 'sleeping' and any(e['type']=='human_message' for e in agent.events))
    saved = agent.store.load_state()
    assert any(m.get('_agent_message_id')==queued['queued'] for m in saved['messages'])
    assert 'Your role is MASTER;' in saved['messages'][0]['content']
    child=team.request('master','create_agent',{'name':'Isolated peer'})['agent_id']
    assert team.agents[child].team is team
    team.request('master','send_agent_message',{'agent_id':child,'message':'Review the result'})
    wait(lambda: any(m['source']==child for m in team.conversation()['messages']))
    team.stop()
    wait(lambda: agent.status == 'stopped')
    queued=agent.submit('Keep this unread while stopped')
    assert team.pending('master',[m.get('_agent_message_id') for m in agent.store.load_state()['messages']])[-1]['content']=='Keep this unread while stopped'
finally:team.close()
team=open_team();agent=team.master
try:
    team.start()
    wait(lambda: agent.status=='sleeping' and any(e['type']=='user_input_applied' and e['message_id']==queued['queued'] for e in agent.events))
    restored=agent.store.load_state()
    assert restored['messages'][:len(saved['messages'])]==saved['messages']
    assert restored['iteration']>saved['iteration']
    assert set(team.agents)=={'master'}, 'Resume must not automatically start peers'
finally:team.close()
assert not any(name.split('.')[0] in {'agent','multiagent','session','node','recoding','recoding_web','recoding_cli','tools'} for name in sys.modules)
print('Independent runtime: Master/sub, tools, messages, sleep, stop and resume passed')
'''


class RuntimePackageTests(unittest.TestCase):
    def test_copied_runtime_package_runs_without_the_project(self):
        # The copied code must remain outside the project; its runtime data
        # follows the shared sessions/tests policy independently.
        with tempfile.TemporaryDirectory(prefix="recoding-agent-code-") as directory,\
                temporary_workspace(prefix="recoding-independent-agent-") as state_directory:
            source = Path(__file__).resolve().parents[1] / "core" / "multiagent"
            shutil.copytree(source, Path(directory) / "independent_agent", ignore=shutil.ignore_patterns("__pycache__"))
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith("GEMINI_") and key != "PYTHONPATH"}
            result = subprocess.run([sys.executable, "-I", "-B", "-c", PROGRAM, directory, state_directory], cwd=directory,
                                    env=environment, capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("Independent runtime:", result.stdout)


if __name__ == "__main__":
    unittest.main()
