"""Host-side projection of a Node ProcessPool; no language queues or ownership authority."""
from copy import deepcopy
from pathlib import Path
import threading
import time
import uuid
from .storage import atomic_json
from .records import browse_pool,TERMINAL
from .protocol import validate_arguments

class PoolClient:
    def __init__(self,store,config,*,language,schemas,environment,actor_info,notify,observe):
        self.store,self.config,self.language,self.environment=store,config,language,environment
        self.actor_info,self.notify,self.observe=actor_info,notify,observe
        self.parameters={s['function']['name']:s['function']['parameters'] for s in schemas};self.names=set(self.parameters)
        self.execution_tool=language;self.data=browse_pool(store,language)
        self.condition=threading.Condition(threading.RLock());self.delivery=threading.Lock();self.lifecycle=threading.RLock()
        self.closed=False;self.halt=threading.Event();self.monitor=None;self.cursor=0;self.sandbox=None;self.maximum=32
        self._save()

    def _save(self):
        for category in ('processes','executions'):
            for r in self.data[category].values():
                r.pop('participants',None);r.update(node_id=self.config.get('node_id','local'),node_name=self.config.get('node_name',''))
        for category in ('executions','processes'):
            ended=sorted((r for r in self.data[category].values() if r['status'] in (TERMINAL if category=='executions' else {'closed','failed'})),key=lambda r:r.get('created_at',0))
            for r in ended[:-1000]:self.data[category].pop(r['id'],None)
        try:
            atomic_json(self.store.folder/(self.language+'-pool.json'),self.data)
        except FileNotFoundError:
            if self.store.folder.exists():raise
            # An externally removed archive must not prevent process cleanup.

    def _actor(self,source):
        actor=self.actor_info(source)
        if not actor or actor['status'] not in {'starting','running','sleeping'}:raise ValueError('Agent is not accepting tools')
        for flag in ('network','gpu'):
            if self.config.get(flag,True) and not actor.get('permissions',{}).get(flag,True):raise ValueError('Agent '+flag+' permission does not cover Node environment')
        return {'id':source,'admin':source=='master','epoch':actor.get('epoch',0)}

    def _ensure(self):
        with self.lifecycle:
            if self.closed:raise ValueError('Process Pool stopped; Resume Session first')
            sandbox=self.environment.ensure()
            if sandbox is not self.sandbox:
                self.sandbox=sandbox;self.cursor=0;self.halt.clear()
                self.monitor=threading.Thread(target=self._monitor,args=(sandbox,),daemon=True,name=self.language+'-node-projection');self.monitor.start()
            return sandbox

    def _rpc(self,operation,*,cancelled=None,**fields):
        result=self._ensure().session.operation('pool',operation=operation,language=self.language,cancelled=cancelled,wait_on_cancel=operation=='create',**fields)
        if result.get('error'):
            if result.get('unknown') or result.get('exit_code')==-9:raise RuntimeError(result['error'])
            # Failed/cancelled creation also emits authoritative cleanup records.
            # Publish them before returning its error to the calling Agent.
            self.refresh()
            raise ValueError(result['error'])
        self.refresh()
        return result

    def refresh(self):
        sandbox=self.sandbox
        if not sandbox:return
        result=sandbox.session.request('pool',operation='updates',language=self.language,after=self.cursor,timeout=3)
        if result.get('error'):raise RuntimeError(result['error'])
        notices=[];observations=[];unresponsive=[]
        with self.condition:
            for update in result.get('updates',[]):
                kind,r=update['kind'],deepcopy(update['record']);category='processes' if kind=='process' else 'executions'
                previous=self.data[category].get(r['id'],{})
                if r.get('revision',0)<=previous.get('revision',-1):continue
                if previous.get('notice_id'):r['notice_id']=previous['notice_id']
                if previous.get('failure_notice'):r['failure_notice']=previous['failure_notice']
                self.data[category][r['id']]=r;observations.append((kind,r))
                if kind=='execution' and r['status'] in TERMINAL and r.get('mode')=='async' and not r.get('notice_id'):notices.append(r['id'])
                if kind=='execution' and r['status']=='unresponsive' and not r.get('failure_notice'):
                    r['failure_notice']=True
                    unresponsive.append(deepcopy(r))
            self.cursor=max(self.cursor,result.get('cursor',0))
            if observations:self._save()
            self.condition.notify_all()
        for kind,r in observations:self.observe(kind,r)
        for r in unresponsive:self.notify('master',self.language+' execution '+r['id']+' did not respond to cancellation. Its Owner or Master may delete the process; partial changes remain.',{'event':'process_unresponsive','execution_id':r['id'],'process_id':r['process_id']})
        for identifier in notices:self._notice(identifier)

    def _notice(self,identifier):
        with self.delivery:
            with self.condition:
                r=deepcopy(self.data['executions'].get(identifier,{}))
                if not r or r.get('notice_id'):return
            content=self.language+' execution '+identifier+': '+r['status']+'; exit_code='+str(r.get('exit_code'))+'.'
            try:
                result=self.output(r,max_bytes=2048)
                for stream in ('stdout','stderr'):
                    if result.get(stream):content+='\n'+stream+':\n'+result[stream]
                if result.get('stdout_has_more') or result.get('stderr_has_more'):content+='\n[More output in execution logs]'
            except (OSError,ValueError,RuntimeError):pass
            notice=self.notify(r.get('submitted_by',r['source']),content,{'event':self.language+'_execution_finished','execution_id':identifier,'process_id':r['process_id']})
            with self.condition:
                if identifier in self.data['executions']:self.data['executions'][identifier]['notice_id']=notice;self._save()

    def _monitor(self,sandbox):
        while not self.halt.wait(.1):
            if sandbox is not self.sandbox:return
            try:self.refresh()
            except (OSError,ValueError,RuntimeError) as error:
                if self.config.get('remote_node'):self.halt.wait(.5);continue
                if not self.closed:self._lost(str(error))
                return

    def output(self,record,**options):return {**self.environment.output(self.language,record,**options),'execution':deepcopy(record)}
    def snapshot(self):
        with self.condition:return deepcopy(self.data)

    def request(self,source,name,fields,*,cancelled=None):
        validate_arguments(fields,self.parameters[name]);actor=self._actor(source)
        allowed=self.actor_info(source).get('permissions',{}).get('tools')
        if source!='master' and allowed is not None and (name not in allowed or self.language not in allowed):raise ValueError('Tool outside delegated permissions')
        if cancelled and cancelled():raise ValueError('Tool request cancelled')
        action=fields.get('action');prefix=self.language
        if name==prefix+'_process':
            if action=='create':
                options={k:fields[k] for k in ('cwd','env','ssh_argv') if k in fields}
                result=self._rpc('create',actor=actor,process_id=uuid.uuid4().hex,name=fields.get('name',prefix.title()),options=options,startup_timeout=fields.get('timeout',30),cancelled=cancelled)
                if cancelled and cancelled():
                    self._rpc('delete',actor=actor,process_id=result['process']['id'])
                    raise ValueError('Process creation cancelled')
                return result
            if action=='list':self._ensure();self.refresh();return {'processes':list(self.snapshot()['processes'].values()),'maximum':32}
            if action=='transfer':
                target=self.actor_info(fields.get('new_owner'))
                if not target or target['status'] not in {'starting','running','sleeping'}:raise ValueError('New Owner must be a live Agent')
            options={k:fields[k] for k in ('process_id','name','new_owner') if k in fields}
            if action=='transfer':options['new_owner_epoch']=target.get('epoch',0)
            return self._rpc(action,actor=actor,**options)
        if name==prefix:
            mode=fields.get('mode','sync');timeout=fields.get('timeout',self.config.get('tool_timeout',30) if mode=='sync' else 0)
            if self.language=='bash' and fields.get('process_id') and set(fields)&{'cwd','env','keep_stdin_open'}:raise ValueError('Persistent Bash uses its own cwd/env and reserved stdin')
            if fields.get('keep_stdin_open') and mode!='async':raise ValueError('keep_stdin_open requires async')
            identifier=uuid.uuid4().hex
            arguments={'actor':actor,'execution_id':identifier,'mode':mode,'execution_timeout':timeout,'code':fields['command' if prefix=='bash' else 'code'],
                       'options':{k:fields[k] for k in ('cwd','env','keep_stdin_open') if k in fields},'name':fields.get('name',prefix.title())}
            if fields.get('process_id'):arguments['process_id']=fields['process_id']
            else:arguments['temporary_id']=uuid.uuid4().hex
            try:result=self._rpc('submit',**arguments)
            except (OSError,RuntimeError) as error:
                if not self.config.get('remote_node'):raise
                record={'id':identifier,'kind':prefix,'process_id':fields.get('process_id',arguments.get('temporary_id')),'source':source,'submitted_by':source,'owner':source,
                        'status':'unknown','mode':mode,'lifetime':'persistent' if fields.get('process_id') else 'temporary','name':arguments['name'],'created_at':time.time(),'error':str(error),'reason':'Submission not acknowledged; query this ID. No code was replayed.'}
                with self.condition:self.data['executions'][identifier]=record;self._save()
                return {'execution':deepcopy(record),'unknown':True,'error':str(error),'exit_code':1}
            if mode=='async':return result
            while True:
                self.refresh()
                with self.condition:
                    record=deepcopy(self.data['executions'].get(identifier,result['execution']))
                    terminal=record['status'] in TERMINAL or record['status']=='unresponsive'
                    reclaimed=record.get('lifetime')!='temporary' or self.data['processes'].get(record['process_id'],{}).get('status') in {'closed','failed'}
                    if terminal and reclaimed:break
                    self.condition.wait(.05)
                if cancelled and cancelled():
                    self._rpc('cancel',actor=actor,execution_id=identifier)
                    return {'execution':record,'cancelled':True,'exit_code':130}
            budget=self.config.get('max_output',16000)
            output=self.output(record,max_bytes=min(64000,max(128,budget)))
            remaining=budget
            for stream in ('stdout','stderr'):
                data=output.get(stream,'').encode()
                if len(data)>remaining:
                    text=data[:remaining].decode('utf-8','ignore')
                    output[stream]=text;output[stream+'_offset']=len(text.encode());output[stream+'_has_more']=True
                remaining-=len(output.get(stream,'').encode())
            return {**output,'exit_code':record.get('exit_code',130),'pending':record['status']=='unresponsive','timed_out':record['status']=='timed_out',
                    'truncated':output.get('stdout_has_more',False) or output.get('stderr_has_more',False)}
        if action=='list':
            self._ensure();self.refresh();rows=self.snapshot()['executions'].values()
            return {'executions':[r for r in rows if not fields.get('process_id') or r['process_id']==fields['process_id']][-100:]}
        identifier=fields.get('execution_id')
        if action=='status':
            self._ensure();self.refresh()
            with self.condition:record=deepcopy(self.data['executions'].get(identifier))
            if record:return {'execution':record}
            raise ValueError('Unknown execution ID')
        if action in {'cancel','write'}:
            return self._rpc(action,actor=actor,execution_id=identifier,**{k:fields[k] for k in ('text','close_stdin') if k in fields})
        if action=='read':
            self._ensure();self.refresh();record=self.data['executions'].get(identifier)
            if record is None:raise ValueError('Unknown execution ID')
            return self.output(record,**{k:fields[k] for k in ('stdout_offset','stderr_offset','max_bytes') if k in fields})
        raise ValueError('Unknown process tool action')

    def stop_owner(self,source,*,epoch=None):
        if self.closed or self.sandbox is None:return []
        actor=self.actor_info(source) or {}
        result=self._rpc('stop_owner',owner=source,epoch=actor.get('epoch',0) if epoch is None else epoch)
        return [r['id'] for r in result.get('processes',[])]

    def _lost(self,reason,*,closing=False):
        with self.condition:
            observations=[]
            for r in self.data['processes'].values():
                if r['status'] not in {'closed','failed'}:
                    r.update(status='closed' if closing else 'failed',reason=reason,active_execution=None,queued=0);observations.append(('process',deepcopy(r)))
            for r in self.data['executions'].values():
                if r['status'] not in TERMINAL:
                    r.update(status='interrupted',reason=reason,memory_lost=True,finished_at=time.time());observations.append(('execution',deepcopy(r)))
            self._save();self.condition.notify_all()
        for kind,r in observations:self.observe(kind,r)

    def reopen(self):self.closed=False;self.halt.clear()
    def close(self):
        self.closed=True;self.halt.set()
        if self.monitor and self.monitor is not threading.current_thread():self.monitor.join(timeout=3)
        self.sandbox=None
