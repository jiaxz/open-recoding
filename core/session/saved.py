"""Non-owning, read-only archive projection. No workers, queues or writebacks."""

from copy import deepcopy
from functools import wraps

from multiagent.events import preview, inventory_snapshot
from multiagent.storage import read_json, load_team
from multiagent.messages import message_page
from multiagent.storage import MemberStore
from multiagent.events import METADATA, update_run, summary
from .store import SessionStore
from .hosting import SessionHostProtocol
from .metrics import speed_snapshot
from multiagent.model_config import default_model
from node.logs import read_saved_execution
from node.records import browse_pool
from .executions import visible_executions


def reading(method):
    @wraps(method)
    def read(self,*args,**kwargs):
        lease=SessionHostProtocol.read(self.store.folder) if self.enforce_host_protocol else None
        try:
            self.store=SessionStore(self.store.folder)
            return method(self,*args,**kwargs)
        finally:
            if lease:lease.close()
    return read


class SavedSession:
    def __init__(self,directory,*,enforce_host_protocol=False):
        self.enforce_host_protocol=enforce_host_protocol
        lease=SessionHostProtocol.read(directory) if enforce_host_protocol else None
        try:self.store=SessionStore(directory)
        finally:
            if lease:lease.close()
        self._cache_stamp=self._cache=None

    @property
    def id(self):return self.store.id
    @property
    def name(self):return self.store.name
    @property
    def status(self):return 'stop'

    def _team_data(self):
        path=self.store.folder/'agents/index.json'
        stamp=(path.stat().st_mtime_ns,path.stat().st_size) if path.exists() else None
        if self._cache is not None and self._cache_stamp==stamp:return self._cache
        data=load_team(self.store)
        self._cache_stamp,self._cache=stamp,data
        return data

    def _agents(self,data):
        result=[]
        for item in data.get('agents',[]):
            row={key:deepcopy(value) for key,value in item.items() if key in METADATA}
            row.update(status='failed' if item.get('status')=='failed' else 'stopped',resumable=True,read_only=True)
            row.pop('sleep',None);result.append(row)
        return result

    @reading
    def agents(self):
        data=self._team_data()
        return [{'id':'master','name':'Master','icon':data['master']['icon'],'status':'stopped',
                 'model':self.store.settings().get('model',default_model()),'read_only':True}]+self._agents(data)

    @reading
    def messages(self,**page):return message_page(self._team_data(),**page)

    def _pool_resources(self,kind):
        from types import SimpleNamespace
        result=browse_pool(self.store,kind)
        path=self.store.folder/'nodes'
        if path.is_dir() and not path.is_symlink():
            for folder in path.iterdir():
                if folder.is_dir() and not folder.is_symlink():
                    data=browse_pool(SimpleNamespace(folder=folder),kind)
                    for category in ('processes','executions'):result[category].update(data[category])
        return result

    @reading
    def python_resources(self):return self._pool_resources('python')

    @reading
    def bash_resources(self):return self._pool_resources('bash')

    def _nodes(self):
        path=self.store.folder/'remote-nodes.json'
        nodes=read_json(path,1024*1024) if path.exists() else {}
        return [{'id':'local','workspace':str(self.store.workspace),'status':'stopped'},
                *[{'id':key,'name':value['name'],'workspace':value['workspace'],'status':'stopped','execution_mode':value.get('execution_mode','sandbox')} for key,value in nodes.items()]]

    @reading
    def list_executions(self,agent_id='master'):
        self.agent_store(agent_id)
        return visible_executions([self.bash_resources(), self.python_resources()], agent_id)

    @reading
    def execution_output(self,agent_id,execution_id,**options):
        self.agent_store(agent_id)
        for kind,data in [('bash',self.bash_resources()),('python',self.python_resources())]:
            record=data['executions'].get(execution_id)
            if record:
                if record.get('node_id','local')!='local':
                    from node.log_cache import read
                    return read(self.store.folder/'nodes'/record['node_id'],record,**options)
                if kind=='python':
                    from tools.python.storage import read_output
                    return read_output(self.store,record,**options)
                result=read_saved_execution(self.store.workspace,self.store.id,execution_id,**options);result.update(execution=record);return result
        raise ValueError('Execution not found')

    @reading
    def output_speed(self):
        data=self._team_data()
        return speed_snapshot(self.store.load_model_rates(),[rate for agent in data.get('agents',[]) for rate in agent.get('rates',[])],
                              started_at=self.store.metadata['created_at'],live=False)

    @reading
    def snapshot(self,*,events=True):
        settings=self.store.settings();data=self._team_data();agents=self._agents(data)
        shown=[preview(event) for event in self.store.load_events()] if events else []
        team=summary({**data,'agents':agents},settings.get('max_subagents',16),settings.get('idle_sleep_seconds',10))
        value={'nodes':self._nodes(),**self.store.info(),'execution_mode':settings.get('execution_mode','sandbox'),'settings':settings,'config':settings,'agents':agents,'team':team,
               'agent_token_usage':deepcopy(data.get('token_usage',{})),
               'master_icon':data['master']['icon'],'status':'stop','master_status':'stopped','read_only':True,'live':False,
               'session_directory':str(self.store.folder),'sequence':shown[-1]['sequence'] if shown else 0,
               'context':next((event['context'] for event in reversed(shown) if isinstance(event.get('context'),dict)),None),
               'tool_inventory':inventory_snapshot(self.store),'executions':self.list_executions()['executions'],'python_pool':self.python_resources(),'bash_pool':self.bash_resources(),'network':settings.get('network',True),
               'gpu_requested':settings.get('gpu',True),'active_model':None,'sleep':None,'run_id':None,
               'max_subagents':team['max_subagents'],'idle_sleep_seconds':team['idle_sleep_seconds'],'error':None}
        if events:value.update(events=shown,model_rates=self.output_speed()['samples'],
                               history_truncated=bool(shown and shown[0]['sequence']>1))
        return value

    @reading
    def agent_store(self,agent_id):
        if agent_id=='master':return self.store,{'id':'master','name':'Master','status':'stopped','resumable':True}
        descriptor=next((agent for agent in self._agents(self._team_data()) if agent['id']==agent_id),None)
        if descriptor is None:raise KeyError('Agent not found')
        return MemberStore(self.store,agent_id),descriptor

    @reading
    def agent_snapshot(self,agent_id):
        if agent_id=='master':return self.snapshot()
        store,descriptor=self.agent_store(agent_id)
        events=[preview(event) for event in store.load_events()]
        return {**descriptor,'descriptor':descriptor,'agent_id':agent_id,'session_directory':str(store.folder),
                'events':events,'sequence':events[-1]['sequence'] if events else 0,
                'history_truncated':bool(events and events[0]['sequence']>1),'tool_inventory':inventory_snapshot(store) or inventory_snapshot(self.store),'executions':self.list_executions(agent_id)['executions'],
                'model_rates':store.load_model_rates(),'read_only':True}

    @reading
    def copy_archive(self,destination):
        import shutil
        shutil.copytree(self.store.folder,destination,ignore=shutil.ignore_patterns('owner.lock','host.lock','cli-runtime.json','cli.log'))

    def _readonly(self,*args,**kwargs):raise ValueError('Session 处于 stop 只读状态，请先 Resume；设置只能暂存到当前界面')
    edit=submit=configure=rename=_readonly
