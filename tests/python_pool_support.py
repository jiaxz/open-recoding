"""Test-only explicit Pool client for package/device boundary fixtures."""
import atexit,uuid
from unittest.mock import patch
from workspace_support import temporary_workspace
from multiagent.config import configuration
from session import SessionStore
from tools.python import PythonTools

_clients={}

class PoolClient:
    def __init__(self,sandbox):
        self.sandbox=sandbox
        store=SessionStore.create(sandbox.directory,'Pool boundary fixture',{})
        config=configuration({'network':sandbox.network,'gpu':sandbox.gpu.metadata()['enabled'],'tool_timeout':sandbox.timeout,'max_output':sandbox.max_output,
            'package_directory':str(sandbox.package_directory),'hidden_paths':[str(p) for p in sandbox.hidden_paths],
            'private_files':[str(p) for p in sandbox.hidden_files]})
        self.pool=PythonTools(store,config,actor_info=lambda source:{'id':'master','status':'running','permissions':{}},
            notify=lambda *args:uuid.uuid4().hex,observe=lambda *args:None)
        self.identifier=None
    def fresh(self):
        if self.identifier:self.pool.request('master','python_process',{'action':'delete','process_id':self.identifier})
        with patch('node.backends.sandbox.user_site_mount',return_value=self.sandbox.user_site):
            process=self.pool.request('master','python_process',{'action':'create','name':'boundary'})['process']
        self.identifier=process['id'];return {'exit_code':0,'process':process}
    def run(self,code):
        if not self.identifier:self.fresh()
        return self.pool.request('master','python',{'process_id':self.identifier,'code':code,'mode':'sync'})

def client(sandbox):
    if sandbox not in _clients:_clients[sandbox]=PoolClient(sandbox)
    return _clients[sandbox]

def run_python(sandbox,code):return client(sandbox).run(code)
def fresh_python(sandbox):return client(sandbox).fresh()
def close_pools():
    for item in list(_clients.values()):item.pool.close()
    _clients.clear()
atexit.register(close_pools)
