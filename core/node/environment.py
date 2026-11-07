"""One execution environment per Session/node, shared by all tools on that node."""
from concurrent.futures import ThreadPoolExecutor
import threading

from .protocol import validate_execution_config
from .backends.sandbox import Sandbox
from .backends.host import HostExecution
from .packages import package_directory
from .inventory import ToolInventoryMonitor

class ExecutionEnvironment:
    def __init__(self,store,config,*,inventory=None,sources,registry_factory):
        self.store,self.config=store,config
        self.sources,self.registry_factory=sources,registry_factory
        validate_execution_config(config)
        self.mode=config.get('execution_mode','sandbox')
        self.inventory=inventory or (lambda report:None)
        self.lock=threading.RLock();self.halt=threading.Event()
        self.sandbox=None;self.launcher=None;self.monitor=None;self.closed=False;self.generation=0
        self.last_inventory=None

    def ensure(self):
        with self.lock:
            if self.closed:raise ValueError('Session execution environment is stopped')
            if self.sandbox is not None:
                if self.sandbox.session.is_alive():return self.sandbox
                if self.config.get('remote_node'):raise RuntimeError('Remote node is disconnected; reconnect and query existing executions. No work was replayed.')
                raise RuntimeError('Session sandbox failed; stop/resume the Session to recreate it. No tools were replayed.')
            self.halt.clear()
            from .build import pool_source
            source=pool_source(self.sources)
            self.launcher=ThreadPoolExecutor(max_workers=1,thread_name_prefix='session-sandbox-owner')
            def launch():
                if self.config.get('remote_node'):
                    from .backends.remote import RemoteBackend
                    return RemoteBackend(self.config['remote_node'],self.config,node_id=self.config['node_id'],owner=self.store.id,sources=self.sources,registry_factory=self.registry_factory)
                backend=Sandbox if self.mode=='sandbox' else HostExecution
                sandbox=backend(self.store.workspace,self.config.get('tool_timeout',30),self.config.get('max_output',16000),
                    network=self.config.get('network',True),gpu=self.config.get('gpu',True),
                    hidden_paths=self.config.get('hidden_paths',[str(self.store.folder.parent)]),hidden_files=self.config.get('private_files',()),
                    package_directory=self.config.get('package_directory',str(package_directory(self.store.workspace))),
                    execution_owner=self.store.id,service_source=source,
                    registry=self.registry_factory(self.config.get('non_builtin_tools',()),isolated=self.mode=='sandbox'),
                    image_source=self.sources['image'])
                try:
                    result=sandbox.session.request('check',cancelled=self.halt.is_set)
                    if result.get('stdout','').strip()!='sandbox-ok':raise RuntimeError(result.get('error','Sandbox check failed'))
                except BaseException:sandbox.close();raise
                return sandbox
            try:self.sandbox=self.launcher.submit(launch).result()
            except BaseException:self.launcher.shutdown(wait=True,cancel_futures=True);self.launcher=None;raise
            self.generation+=1
            def updated(report):self.last_inventory=report;self.inventory(report)
            self.monitor=ToolInventoryMonitor(self.sandbox,updated);self.monitor.start()
            return self.sandbox

    def facts(self):
        sandbox=self.ensure()
        return {'workspace':str(self.store.workspace),'network':sandbox.network,'gpu':sandbox.gpu_info,
                'generation':self.generation,'sandbox_pid':sandbox.session.process.pid,'execution_mode':self.mode,
                'paths':dict(sandbox.paths),'tool_workspace':sandbox.tool_workspace,'node_id':self.config.get('node_id','local'),
                'runtime_id':sandbox.session.runtime_id,'status':getattr(sandbox,'status','connected')}

    def alive(self):
        return self.sandbox is not None and self.sandbox.session.is_alive()

    def invoke(self,kind,**fields):
        result=self.ensure().session.request(kind,**fields)
        if result.get('error'):raise RuntimeError(result['error'])
        return result

    def output(self,language,record,**options):
        result=self.invoke('logs',language=language,record=record,options=options)
        raw=result.pop('_raw',{})
        if self.config.get('remote_node'):
            from .log_cache import save
            save(self.store.folder,record,result,raw,options)
        return result

    def refresh(self):
        self.ensure()
        if self.monitor:self.monitor.request()
        return {'queued':True}

    def reopen(self):
        with self.lock:self.closed=False;self.halt.clear()

    def close(self):
        self.closed=True;self.halt.set()
        with self.lock:
            monitor,self.monitor=self.monitor,None
            sandbox,self.sandbox=self.sandbox,None
            if monitor:monitor.close()
            if sandbox:sandbox.close()
            if self.launcher:self.launcher.shutdown(wait=True,cancel_futures=True);self.launcher=None
