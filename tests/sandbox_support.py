"""Explicit test-only assembly of tools and node backends."""
from node.backends.sandbox import Sandbox as NodeSandbox
from node.backends.host import HostExecution as NodeHost
from tools import definitions,execution_sources
from tools.registry import ToolRegistry

def assemble(backend,*args,**kwargs):
    records=kwargs.pop('non_builtin_tools',())
    kwargs['registry']=ToolRegistry(records,isolated=backend.mode=='sandbox')
    kwargs['image_source']=execution_sources()['image']
    from node.build import pool_source
    kwargs['service_source']=pool_source(execution_sources())
    instance=backend(*args,**kwargs)
    return connect_process_tools(instance,records)

def Sandbox(*args,**kwargs):return assemble(NodeSandbox,*args,**kwargs)
def HostExecution(*args,**kwargs):return assemble(NodeHost,*args,**kwargs)
def tool_sandbox(*args,**kwargs):
    kwargs.setdefault('non_builtin_tools',definitions())
    return Sandbox(*args,**kwargs)

def connect_process_tools(backend,records):
    """Assemble the same Host tool port as Session for low-level backend tests."""
    from types import SimpleNamespace
    from pathlib import Path
    import json,uuid
    from node.pool_client import PoolClient
    from node.protocol import validate_arguments
    from tools.python.schemas import SCHEMAS as PYTHON
    from tools.bash.schemas import SCHEMAS as BASH
    original_execute,original_close=backend.execute,backend.close
    clients={}
    schemas={s['function']['name']:s['function']['parameters'] for s in [*PYTHON,*BASH]}
    enabled={r['schema']['function']['name'] for r in records}
    store=SimpleNamespace(id=backend.execution_owner,workspace=backend.directory,
        folder=Path(backend.directory)/'.recoding/runtime/test-projection'/backend.execution_owner)
    def output(language,record,**options):
        result=backend.session.request('logs',language=language,record=record,options=options)
        result.pop('_raw',None)
        if result.get('error'):raise ValueError(result['error'])
        return result
    environment=SimpleNamespace(ensure=lambda:backend,output=output)
    def execute(name,arguments,*,cancelled=None):
        if name not in schemas or name not in enabled:return original_execute(name,arguments)
        try:
            fields=json.loads(arguments);validate_arguments(fields,schemas[name])
            language=name.split('_')[0]
            if language not in clients:
                store.folder.mkdir(mode=0o700,parents=True,exist_ok=True)
                clients[language]=PoolClient(store,{'network':backend.network,'gpu':backend.gpu.metadata()['enabled'],'tool_timeout':backend.timeout,'max_output':backend.max_output},
                    language=language,schemas=PYTHON if language=='python' else BASH,environment=environment,
                    actor_info=lambda source:{'status':'running','permissions':{}},notify=lambda *args:uuid.uuid4().hex,observe=lambda *args:None)
            clients[language].config.update(tool_timeout=backend.timeout,max_output=backend.max_output)
            result=clients[language].request('master',name,fields,cancelled=cancelled)
            result.setdefault('session_state',backend.session_state)
            return result
        except (ValueError,RuntimeError,OSError) as error:return {'error':str(error),'exit_code':1}
    def close():
        for client in clients.values():client.close()
        return original_close()
    backend.execute=execute;backend.close=close
    return backend
