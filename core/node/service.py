"""Session gateway over per-node execution environments, process pools and a role-aware API."""
from copy import deepcopy
import threading
from .environment import ExecutionEnvironment
from .protocol import validate_arguments
from .records import TERMINAL

class NodeResources:
    def __init__(self,store,config,*,actor_info,notify,observe,adapters,sources,registry_factory):
        self.store,self.config=store,config;self.actor_info,self.notify,self.observe=actor_info,notify,observe
        self.definitions=config.get('non_builtin_tools',[])
        self.parameters={r['schema']['function']['name']:r['schema']['function']['parameters'] for r in self.definitions}
        self.names=set(self.parameters)|{'execution_environment','inspect_tools'}
        self.environment=ExecutionEnvironment(store,config,sources=sources,registry_factory=registry_factory,inventory=lambda r:observe('environment','inventory',r))
        self.python_pool=adapters['python'](store,config,environment=self.environment,actor_info=actor_info,notify=notify,observe=lambda k,r:observe('python',k,r))
        self.bash_pool=adapters['bash'](store,config,environment=self.environment,actor_info=actor_info,notify=notify,observe=lambda k,r:observe('bash',k,r))
        self.lock=threading.RLock();self.halt=threading.Event();self.monitor=None;self.closed=False;self.pending_stops=set()

    def ensure(self):
        if self.closed:raise ValueError('Session execution environment stopped')
        sandbox=self.environment.ensure()
        with self.lock:
            if self.monitor is None or not self.monitor.is_alive():
                self.halt.clear();self.monitor=threading.Thread(target=self.watch,daemon=True,name='session-execution-observer');self.monitor.start()
        return sandbox

    def watch(self):
        while not self.halt.wait(.1):
            sandbox=self.environment.sandbox
            try:
                if sandbox is None:return
                if not sandbox.session.is_alive():raise RuntimeError('Session sandbox exited')
                self.flush_stops()
                if self.config.get('remote_node') and getattr(self,'connection_state',None)!='connected':
                    self.connection_state='connected';self.observe('node','state',{'status':'connected'})
            except (OSError,ValueError,RuntimeError) as error:
                if self.config.get('remote_node') and not self.halt.is_set():
                    if getattr(self,'connection_state',None)!='disconnected':
                        self.connection_state='disconnected';self.observe('node','state',{'status':'disconnected','error':str(error)})
                        self.notify('master','Node '+self.config['node_name']+' disconnected. Existing executions are unknown; query their IDs after reconnect and do not blindly replay work.',{'event':'node_disconnected','node_id':self.config['node_id']})
                    self.halt.wait(.5);continue
                if not self.halt.is_set():
                    self.bash_pool._lost(str(error));self.python_pool._lost(str(error))
                    self.notify('master','Session execution environment failed: '+str(error)+'. No tools were replayed; stop/resume the Session to recreate it.',{'event':'execution_environment_failed'})
                return

    def request(self,source,name,fields,*,cancelled=None):
        actor=self.actor_info(source)
        if actor is None or actor['status'] not in {'starting','running','sleeping'}:raise ValueError('Agent is not accepting tool requests')
        if name in {'execution_environment','inspect_tools'}:
            self.ensure()
            return self.environment.facts() if name=='execution_environment' else self.environment.refresh()
        if name not in self.parameters:raise ValueError('Tool is not installed')
        validate_arguments(fields,self.parameters[name])
        allowed=actor.get('permissions',{}).get('tools')
        if source!='master' and allowed is not None and name not in allowed:raise ValueError('Tool outside delegated permissions')
        self.ensure()
        if name in self.python_pool.names:return self.python_pool.request(source,name,fields,cancelled=cancelled)
        if name in self.bash_pool.parameters:return self.bash_pool.request(source,name,fields,cancelled=cancelled)
        permissions=actor.get('permissions',{})
        for setting in ('network','gpu'):
            if self.config.get(setting,True) and not permissions.get(setting,True):raise ValueError('Agent '+setting+' permission does not cover the shared Session sandbox')
        if cancelled and cancelled():return {'cancelled':True,'exit_code':130}
        # The registry validates arguments/source and uses nonblocking operations.
        import json
        return self.environment.sandbox.registry.execute(self.environment.sandbox.session,name,json.dumps(fields),cancelled=cancelled)

    def stop_owner(self,source):
        if self.closed or source=='master':return
        actor=self.actor_info(source) or {}
        with self.lock:self.pending_stops.add((source,actor.get('epoch',0)))
        self.flush_stops()

    def flush_stops(self):
        client=next((p for p in (self.python_pool,self.bash_pool) if p.sandbox is not None),None)
        if client is None:return
        with self.lock:pending=list(self.pending_stops)
        for source,epoch in pending:
            try:client.stop_owner(source,epoch=epoch)
            except (OSError,ValueError,RuntimeError):
                # Global teardown supersedes this operation; a disconnected live
                # node retries it on reconnect. Never strand an Agent cleaner.
                return
            with self.lock:self.pending_stops.discard((source,epoch))

    def reopen(self):
        self.closed=False;self.pending_stops.clear();self.environment.reopen();self.python_pool.reopen();self.bash_pool.reopen()
    def close(self):
        if self.closed:return
        self.closed=True;self.halt.set()
        try:
            self.bash_pool.close();self.python_pool.close()
        finally:
            backend=self.environment.sandbox
            self.environment.close()
            confirmed=not self.config.get('remote_node') or backend is None or getattr(backend,'status',None)=='stopped'
            for client in (self.python_pool,self.bash_pool):
                if confirmed:client._lost('Session NodeRuntime closed',closing=True)
        if self.monitor and self.monitor is not threading.current_thread():self.monitor.join(timeout=3)


class ExecutionService:
    """Session-wide gateway over independent per-node runtimes and tool adapters."""
    def __init__(self,store,config,*,actor_info,notify,observe,adapters,sources,registry_factory):
        self.store,self.config,self.actor_info,self.notify,self.observe=store,config,actor_info,notify,observe
        self.adapters,self.sources,self.registry_factory=adapters,sources,registry_factory
        self.lock=threading.RLock();self.closed=False;self.pending_nodes={}
        self.parameters={item['schema']['function']['name']:item['schema']['function']['parameters'] for item in config.get('non_builtin_tools',())}
        self.names=set(self.parameters)|{'execution_environment','inspect_tools'}
        self.resources={'local':self._resource('local',None)}
        self.file_transfers=adapters['files'](store,config,notify=notify,observe=lambda k,r:observe('files',k,r),endpoint_factory=self.file_endpoint)
        self.bindings=self.file_transfers.nodes
        for identifier,spec in self.bindings.items():self.resources[identifier]=self._resource(identifier,spec)

    def _resource(self,identifier,spec):
        from pathlib import Path
        from types import SimpleNamespace
        if identifier=='local':store=self.store;config={**self.config,'node_id':'local','node_name':''}
        else:
            folder=self.store.folder/'nodes'/identifier;folder.mkdir(mode=0o700,parents=True,exist_ok=True)
            store=SimpleNamespace(id=self.store.id,folder=folder,workspace=Path(spec['workspace']))
            config={**self.config,'node_id':identifier,'node_name':spec['name'],'remote_node':spec,'execution_mode':spec['execution_mode']}
        def observed(language,kind,record):
            self.observe(language,kind,{**record,'node_id':identifier,'node_name':spec['name'] if spec else ''})
        return NodeResources(store,config,actor_info=self.actor_info,notify=self.notify,observe=observed,
                             adapters=self.adapters,sources=self.sources,registry_factory=self.registry_factory)

    @property
    def environment(self):return self.resources['local'].environment
    @property
    def python_pool(self):return self.resources['local'].python_pool
    @property
    def bash_pool(self):return self.resources['local'].bash_pool
    def ensure(self):return self.resources['local'].ensure()

    def pool_snapshot(self,kind):
        result={'processes':{},'executions':{}}
        for resource in list(self.resources.values()):
            data=getattr(resource,kind+'_pool').snapshot()
            for category in result:result[category].update(data[category])
        return result

    def pools(self):
        return [getattr(resource,kind+'_pool') for resource in list(self.resources.values()) for kind in ('bash','python')]

    def listing(self):
        result=[]
        for identifier,resource in list(self.resources.items()):
            environment=resource.environment;backend=environment.sandbox;spec=self.bindings.get(identifier,{})
            status='stopped' if self.closed else getattr(backend,'status','connected') if backend else 'configured'
            result.append({'id':identifier,'name':spec.get('name',''),'workspace':str(resource.store.workspace),'tool_workspace':backend.tool_workspace if backend else '/workspace' if resource.config['execution_mode']=='sandbox' else str(resource.store.workspace),
                'execution_mode':resource.config['execution_mode'],'status':status,
                'capabilities':['bash','python','files','images','tools'] if backend and status=='connected' else [],
                'resources':getattr(backend,'capabilities',None),'gpu':getattr(backend,'gpu_info',None),
                'tool_score':environment.last_inventory.get('score') if environment.last_inventory else None,
                'runtime_id':backend.session.runtime_id if backend else None,
                'generation':environment.generation})
        return {'nodes':result}

    def configure(self,identifier,spec):
        from .spec import binding
        spec=binding(identifier,spec)
        with self.lock:
            if self.closed:raise ValueError('Resume the Session before adding a node')
            if identifier in self.resources or identifier in self.pending_nodes:raise ValueError('Node already exists or is connecting')
            candidate=self._resource(identifier,spec);done=threading.Event();self.pending_nodes[identifier]=(candidate,done)
        try:
            candidate.ensure()
            with self.lock:
                if self.closed or candidate.environment.halt.is_set():raise ValueError('Node startup cancelled or Session stopped')
                from pathlib import Path
                spec['workspace']=getattr(candidate.environment.sandbox,'workspace',spec['workspace'])
                candidate.store.workspace=Path(spec['workspace'])
                self.file_transfers.configure(identifier,spec);self.resources[identifier]=candidate
        except BaseException:candidate.close();raise
        finally:
            with self.lock:self.pending_nodes.pop(identifier,None);done.set()
        self.observe('node','added',{'node_id':identifier,'node_name':spec['name'],'workspace':spec['workspace'],'status':'connected'})
        return self.listing()

    def cancel_add(self,identifier):
        with self.lock:
            pending=self.pending_nodes.get(identifier)
            if pending:pending[0].environment.halt.set()
        return {'accepted':bool(pending)}

    def connect(self,identifier):
        if self.closed:raise ValueError('Resume the Session first')
        if identifier not in self.resources:raise ValueError('Unknown node')
        resource=self.resources[identifier]
        try:resource.ensure()
        except RuntimeError:
            backend=resource.environment.sandbox
            if identifier=='local' or backend is None or backend.runtime_present():raise
            resource.close();resource.reopen()
            resource.ensure()
            self.notify('master','Node '+self.bindings[identifier]['name']+' started a new Runtime generation. Previous execution memory is gone; no work was replayed. Inspect pool_status and create fresh processes as needed.',{'event':'node_restarted','node_id':identifier})
        return self.listing()

    def remove(self,identifier):
        if identifier=='local':raise ValueError('Local is mandatory')
        with self.lock:
            resource=self.resources.get(identifier)
            if resource is None:raise ValueError('Unknown node')
            if any(record['status'] not in {'closed','failed'} for pool in (resource.python_pool,resource.bash_pool) for record in pool.data['processes'].values()):raise ValueError('Close live node processes before removing this binding')
            resource.close();self.file_transfers.remove_node(identifier);self.resources.pop(identifier)
        return self.listing()

    def file_endpoint(self,identifier):
        from .file_port import NodeFiles
        if self.closed:raise ValueError('Session stopped; no new file endpoint may start')
        if identifier not in self.resources:raise ValueError('Unknown node')
        return NodeFiles(self.resources[identifier].environment)

    def _target(self,fields):
        requested=fields.get('node_id');found=None
        for key,category in (('process_id','processes'),('execution_id','executions')):
            identifier=fields.get(key)
            if not identifier:continue
            for node,resource in self.resources.items():
                if any(identifier in pool.data[category] for pool in (resource.python_pool,resource.bash_pool)):
                    if found and found!=node:raise ValueError('Resource IDs refer to different nodes')
                    found=node
            if found is None:raise ValueError('Unknown resource ID')
        if requested and found and requested!=found:raise ValueError('Resource belongs to a different node')
        target=found or requested or 'local'
        if target not in self.resources:raise ValueError('Unknown node; use list_nodes')
        return target

    def request(self,source,name,fields,*,cancelled=None):
        if self.closed:raise ValueError('Session execution stopped')
        actor=self.actor_info(source)
        if not actor or actor['status'] not in {'starting','running','sleeping'}:raise ValueError('Agent is not accepting tool requests')
        if name not in {'execution_environment','inspect_tools'}:
            if name not in self.parameters:raise ValueError('Tool is not installed')
            validate_arguments(fields,self.parameters[name])
            allowed=actor.get('permissions',{}).get('tools')
            if source!='master' and allowed is not None and name not in allowed:raise ValueError('Tool outside delegated permissions')
        if name=='list_nodes':return self.listing()
        if name=='transfer_files':return self.file_transfers.request(source,fields,cancelled=cancelled,network=self.config.get('network',True) and actor.get('permissions',{}).get('network',True))
        if name=='pool_status':
            records=[];executions=[];counts={}
            for kind in ('bash','python'):
                data=self.pool_snapshot(kind)
                counts[kind]={'processes':len(data['processes']),'active':sum(r['status'] not in {'closed','failed'} for r in data['processes'].values()),'executions':len(data['executions'])}
                if fields.get('kind') and fields['kind']!=kind:continue
                for category,target_rows in (('processes',records),('executions',executions)):
                    for record in data[category].values():
                        if fields.get('node_id') and record.get('node_id')!=fields['node_id']:continue
                        if fields.get('owner') and record.get('owner')!=fields['owner']:continue
                        if fields.get('status') and record.get('status')!=fields['status']:continue
                        if not fields.get('include_closed') and record['status'] in ({'closed','failed'} if category=='processes' else TERMINAL):continue
                        target_rows.append({**record,'kind':kind})
            for rows in (records,executions):rows.sort(key=lambda r:r.get('created_at',0),reverse=True)
            offset=fields.get('offset',0);limit=fields.get('limit',50);total=max(len(records),len(executions))
            return {'nodes':self.listing()['nodes'],'counts':counts,'processes':records[offset:offset+limit],
                    'executions':executions[offset:offset+limit],'has_more':offset+limit<total,'next_offset':offset+limit if offset+limit<total else None}
        target=self._target(fields);resource=self.resources[target]
        if target!='local' and (not self.config.get('network',True) or not actor.get('permissions',{}).get('network',True)):raise ValueError('Remote node requires delegated network access')
        if not fields.get('node_id') and not fields.get('process_id') and not fields.get('execution_id') and fields.get('action')=='list' and name in {'python_process','bash_process','python_execution','bash_execution'}:
            category='processes' if name.endswith('_process') else 'executions'
            rows=[r.request(source,name,fields,cancelled=cancelled) for r in self.resources.values()]
            return {category:[item for row in rows for item in row.get(category,[])]}
        if target!='local':self.observe('node','target',{'source':source,'tool':name,'node_id':target,'node_name':self.bindings[target]['name']})
        result=resource.request(source,name,{k:v for k,v in fields.items() if k!='node_id'},cancelled=cancelled)
        return {**result,'node_id':target,'node_name':self.bindings[target]['name']} if target!='local' else result

    def stop_owner(self,source):
        self.file_transfers.stop_owner(source)
        for resource in list(self.resources.values()):resource.stop_owner(source)

    def reopen(self):
        self.closed=False;self.file_transfers.reopen()
        for resource in self.resources.values():resource.reopen()

    def close(self):
        if self.closed:return
        with self.lock:
            self.closed=True;pending=list(self.pending_nodes.values())
            for candidate,_ in pending:candidate.environment.halt.set()
        self.file_transfers.close()
        for _,done in pending:done.wait()
        for resource in reversed(list(self.resources.values())):resource.close()
