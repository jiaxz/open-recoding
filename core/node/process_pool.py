"""Node-owned process lifecycle and FIFO execution, independent of language and Agents.

Drivers implement start/execute/close (and optionally write). The trusted Host supplies
opaque caller identities; no model-supplied role or owner is accepted as authority.
"""
from collections import deque
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid

TERMINAL={'succeeded','failed','cancelled','timed_out','interrupted'}

class ProcessPool:
    def __init__(self,context,ports,drivers,*,close_grace=5):
        self.context,self.ports,self.drivers=deepcopy(context),ports,drivers
        self.lock=threading.RLock();self.processes={};self.executions={}
        self.events=deque(maxlen=4096);self.sequence=0;self.closed=False
        self.stopped=set();self.close_grace=close_grace
        self.workspace=Path(context['env'].get('RECODING_WORKSPACE','/workspace'))
        self.session=context['env']['RECODING_EXECUTION_OWNER']
        self.halt=threading.Event()
        self.monitor=threading.Thread(target=self.watch,daemon=True,name='node-process-pool')
        self.monitor.start()

    def identity(self,value):
        if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{32}',value):raise ValueError('Invalid resource ID')
        return value

    def caller(self,request,*,live=True):
        actor=request.get('actor',{})
        if not isinstance(actor,dict) or not isinstance(actor.get('id'),str):raise ValueError('Missing trusted caller identity')
        if live and (actor['id'],str(actor.get('epoch',0))) in self.stopped:raise ValueError('Agent attempt has stopped')
        return actor

    def managed(self,actor,process):
        if not actor.get('admin') and actor['id']!=process['record']['owner']:raise ValueError('Only Owner or Master may manage this process')

    def directory(self,path):
        for parent in reversed([path,*path.parents]):
            if not parent.is_relative_to(self.workspace):continue
            if parent.is_symlink():raise ValueError('Runtime storage contains a symlink')
            parent.mkdir(mode=0o700,exist_ok=True)
        return path

    def save(self,record,folder):
        target=folder/'metadata.json';temporary=folder/'.metadata.tmp'
        temporary.write_text(json.dumps(record,ensure_ascii=False));os.replace(temporary,target)

    def emit(self,kind,record):
        record['revision']=record.get('revision',0)+1
        self.sequence+=1
        self.events.append({'sequence':self.sequence,'kind':kind,'record':deepcopy(record)})
        if kind=='execution':self.save(record,self.executions[record['id']]['folder'])
        else:
            folder=self.directory(self.workspace/'.recoding/runtime/process-pool'/self.session/record['id'])
            self.save(record,folder)

    def create(self,kind,request,actor,*,temporary=False):
        if kind not in self.drivers:raise ValueError('Process type is not installed')
        identifier=self.identity(request.get('process_id') or uuid.uuid4().hex)
        options=request.get('options',{});name=request.get('name') or kind.title()
        if not isinstance(name,str) or not 1<=len(name)<=100:raise ValueError('Process name must be 1–100 characters')
        with self.lock:
            self.caller(request)
            if self.closed or identifier in self.processes:raise ValueError('Pool closed or process ID already used')
            if sum(p['record']['status'] not in {'closed','failed'} and p['record']['kind']==kind for p in self.processes.values())>=32:raise ValueError('Process limit reached (32 per type/node)')
            record={'id':identifier,'kind':kind,'owner':actor['id'],'creator':actor['id'],'owner_epoch':str(actor.get('epoch',0)),'name':name,
                    'lifetime':'temporary' if temporary else 'persistent','status':'starting','created_at':time.time(),'active_execution':None,'queued':0}
            item={'record':record,'driver':None,'queue':deque(),'wake':threading.Event(),'closing':False,'retiring':False,'thread':None,'active':None,'closed':threading.Event()}
            self.processes[identifier]=item;self.emit('process',record)
        try:
            driver=self.drivers[kind](deepcopy(self.context),options,temporary,self.ports)
            with self.lock:
                if item['closing'] or item['retiring'] or self.closed:raise RuntimeError('Process creation cancelled')
                item['driver']=driver
            details=driver.start(time.monotonic()+request.get('startup_timeout',30),lambda:item['closing'] or item['retiring'] or request.get('_cancelled',lambda:False)())
            with self.lock:
                if item['closing'] or item['retiring'] or self.closed or request.get('_cancelled',lambda:False)():raise RuntimeError('Process creation cancelled')
                record.update(details,status='ready');self.emit('process',record)
                item['thread']=threading.Thread(target=self.run,args=(item,),daemon=True,name=kind+'-'+identifier[:8]);item['thread'].start()
                return item
        except BaseException:
            if item.get('driver'):item['driver'].close()
            self.delete(item,'Process startup failed or cancelled')
            raise

    def submit(self,kind,request,actor):
        temporary=not request.get('process_id')
        if temporary:item=self.create(kind,{**request,'process_id':request.get('temporary_id')},actor,temporary=True)
        else:
            with self.lock:
                item=self.processes.get(request['process_id'])
                if item is None or item['record']['kind']!=kind:raise ValueError('Unknown process ID/type')
        identifier=self.identity(request.get('execution_id') or uuid.uuid4().hex)
        timeout=request.get('execution_timeout',0)
        if type(timeout) not in (int,float) or not math.isfinite(timeout) or timeout<0:raise ValueError('Invalid execution timeout')
        try:
            with self.lock:
                self.caller(request)
                p=item['record']
                if item['closing'] or item['retiring'] or p['status'] not in {'ready','running'}:raise ValueError('Process is not accepting work')
                if p['lifetime']=='temporary' and not temporary:raise ValueError('Temporary process cannot be reused')
                if len(item['queue'])>=128:raise ValueError('Process queue is full (128 pending)')
                if identifier in self.executions:raise ValueError('Execution ID already used; never replay')
                folder=(self.workspace/'.recoding/runtime/python-pool'/self.session/p['id']/identifier if kind=='python' else self.workspace/'.recoding/runtime/executions'/self.session/identifier)
                self.directory(folder)
                for stream in ('stdout','stderr'):(folder/(stream+'.log')).touch(mode=0o600)
                record={'id':identifier,'process_id':p['id'],'kind':kind,'name':p['name'],'owner':p['owner'],
                    'source':actor['id'],'submitted_by':actor['id'],'submitter_epoch':str(actor.get('epoch',0)),'mode':request.get('mode','sync'),'lifetime':p['lifetime'],
                    'status':'queued','created_at':time.time(),'timeout':timeout,'max_runtime':timeout,'remote':p.get('remote',False),
                    'cwd':p.get('cwd',str(self.workspace)),'stdout_path':str(folder/'stdout.log'),'stderr_path':str(folder/'stderr.log')}
                execution={'record':record,'folder':folder,'code':request['code'],'cancel':threading.Event(),'done':threading.Event()}
                self.executions[identifier]=execution;item['queue'].append(identifier);p['queued']=len(item['queue'])
                self.emit('execution',record);self.emit('process',p);item['wake'].set()
                return {'execution':deepcopy(record)}
        except BaseException:
            if temporary:self.delete(item,'Submission failed')
            raise

    def cancel(self,execution,reason):
        with self.lock:
            r=execution['record'];item=self.processes[r['process_id']]
            if r['status'] in TERMINAL:return deepcopy(r)
            execution['cancel'].set()
            if r['status']=='queued':
                item['queue'].remove(r['id']);item['record']['queued']=len(item['queue'])
                r.update(status='cancelled',exit_code=130,reason=reason,finished_at=time.time());execution.pop('code',None);execution['done'].set()
                self.emit('process',item['record'])
                if r['lifetime']=='temporary':self.retire(item,reason)
            elif r['status'] not in {'cancelling','unresponsive'}:
                r.update(status='cancelling',cancel_requested=True,cancel_requested_at=time.time(),reason=reason)
                if not item['retiring']:item['record']['status']='cancelling';self.emit('process',item['record'])
            self.emit('execution',r)
            return deepcopy(r)

    def retire(self,item,reason):
        # The admission gate and queue drain are atomic with submit/run dequeue.
        with self.lock:
            if item['closing'] or item['retiring']:return
            item['retiring']=True;item['retire_deadline']=time.monotonic()+self.close_grace
            item['record'].update(status='closing',reason=reason)
            for identifier in list(item['queue']):self.cancel(self.executions[identifier],reason)
            self.emit('process',item['record']);item['wake'].set()

    def delete(self,item,reason):
        with self.lock:
            if item['closing']:return
            item['closing']=True;item['retiring']=True;item['wake'].set()
            item['record'].update(status='closing',reason=reason);self.emit('process',item['record'])
            related=[e for e in self.executions.values() if e['record']['process_id']==item['record']['id'] and e['record']['status'] not in TERMINAL]
            for e in related:e['cancel'].set()
            item['queue'].clear()
        try:
            if item['driver']:item['driver'].close()
            thread=item['thread']
            if thread and thread is not threading.current_thread():thread.join(timeout=2)
        finally:
            with self.lock:
                for e in related:
                    if e['record']['status'] not in TERMINAL:
                        e['record'].update(status='cancelled',exit_code=130,reason=reason,memory_lost=True,finished_at=time.time())
                        e.pop('code',None);e['done'].set();self.emit('execution',e['record'])
                item['record'].update(status='closed',closed_at=time.time(),active_execution=None,queued=0)
                self.emit('process',item['record']);item['closed'].set()

    def run(self,item):
        while not item['closing']:
            item['wake'].wait(.05)
            with self.lock:
                if item['closing']:return
                if item['retiring']:
                    if item['active'] is None:break
                    continue
                if not item['queue']:item['wake'].clear();continue
                identifier=item['queue'].popleft();execution=self.executions[identifier];record=execution['record']
                item['active']=identifier;record.update(status='running',started_at=time.time(),pid=item['record'].get('pid'))
                item['record'].update(status='running',active_execution=identifier,queued=len(item['queue']))
                self.emit('execution',record);self.emit('process',item['record'])
            lost=False
            try:
                result=item['driver'].execute(execution['code'],identifier,execution['folder'],execution['cancel'])
                lost=result.get('memory_lost',False)
            except BaseException as error:
                lost=True;result={'exit_code':-1,'error':str(error) or type(error).__name__,'memory_lost':True}
            with self.lock:
                if record['status'] not in TERMINAL:
                    interrupted=bool(result.pop('interrupted',False))
                    status='timed_out' if execution.get('timed_out') and (interrupted or lost) else 'cancelled' if execution['cancel'].is_set() and (interrupted or lost) else 'succeeded' if result.get('exit_code')==0 else 'failed'
                    record.update(result,status=status,finished_at=time.time(),elapsed=time.time()-record['started_at']);execution.pop('code',None)
                    for stream in ('stdout','stderr'):record[stream+'_bytes']=(execution['folder']/(stream+'.log')).stat().st_size
                    self.emit('execution',record)
                execution['done'].set();item['active']=None
                if not item['closing']:
                    item['record'].update(status='closing' if item['retiring'] else 'ready',active_execution=None,queued=len(item['queue']))
                    self.emit('process',item['record'])
                finish=item['retiring'] or item['record']['lifetime']=='temporary' or lost
            if finish:
                self.delete(item,'Owner stopped' if item['retiring'] else 'Execution finished' if not lost else 'Interpreter failed');return
        self.delete(item,'Owner stopped')

    def watch(self):
        while not self.halt.wait(.05):
            close=[]
            with self.lock:
                now=time.time()
                for e in list(self.executions.values()):
                    r=e['record']
                    p=self.processes[r['process_id']]
                    driver=p.get('driver')
                    if r['status']=='running' and not r.get('pid') and driver and driver.process is not None:
                        r['pid']=driver.process.pid;p['record']['pid']=driver.process.pid
                        self.emit('execution',r);self.emit('process',p['record'])
                    if r['status']=='running' and r['timeout'] and now-r['started_at']>=r['timeout']:
                        e['timed_out']=True;self.cancel(e,'Execution deadline exceeded')
                    if r['status']=='cancelling' and now-r['cancel_requested_at']>=2:
                        r['status']='unresponsive';self.emit('execution',r)
                        p=self.processes[r['process_id']]
                        if not p['retiring']:p['record']['status']='unresponsive';self.emit('process',p['record'])
                    if r['status'] in {'cancelling','unresponsive'} and r['lifetime']=='temporary':close.append(self.processes[r['process_id']])
                ended=sorted((e for e in self.executions.values() if e['record']['status'] in TERMINAL),key=lambda e:e['record']['created_at'])
                for e in ended[:-2000]:self.executions.pop(e['record']['id'],None)
                referenced={e['record']['process_id'] for e in self.executions.values()}
                for identifier,p in list(self.processes.items()):
                    if p['closed'].is_set() and identifier not in referenced:self.processes.pop(identifier,None)
                for item in list(self.processes.values()):
                    if item['retiring'] and not item['closing'] and (item['active'] is None or time.monotonic()>=item['retire_deadline']):close.append(item)
            for item in close:self.delete(item,'Close grace elapsed' if item['active'] else 'Owner stopped or execution cancelled')

    def request(self,request):
        action=request['operation'];kind=request.get('language')
        with self.lock:
            if action=='updates':
                after=request.get('after',0)
                if after and self.events and after<self.events[0]['sequence']-1:
                    updates=[{'kind':'process','record':deepcopy(p['record'])} for p in self.processes.values()]+[{'kind':'execution','record':deepcopy(e['record'])} for e in self.executions.values()]
                else:updates=[deepcopy(e) for e in self.events if e['sequence']>after]
                return {'updates':[e for e in updates if not kind or e['record']['kind']==kind],'cursor':self.sequence}
            if action=='stop_owner':
                source=request['owner'];self.stopped.add((source,str(request.get('epoch',0))))
                for e in list(self.executions.values()):
                    if e['record']['submitted_by']==source and e['record']['submitter_epoch']==str(request.get('epoch',0)):self.cancel(e,'Submitting Agent stopped')
                owned=[p for p in self.processes.values() if p['record']['owner']==source and p['record']['owner_epoch']==str(request.get('epoch',0)) and p['record']['status'] not in {'closed','failed'}]
                for item in owned:self.retire(item,'Owner stopped')
                return {'processes':[deepcopy(p['record']) for p in owned]}
            actor=self.caller(request)
            if action in {'create','submit'}:pass
            elif action=='list':
                return {'processes':[deepcopy(p['record']) for p in self.processes.values() if p['record']['kind']==kind],
                        'executions':[deepcopy(e['record']) for e in self.executions.values() if e['record']['kind']==kind]}
            elif action in {'delete','rename','transfer'}:
                item=self.processes.get(request.get('process_id'))
                if not item or item['record']['kind']!=kind:raise ValueError('Unknown process ID/type')
                self.managed(actor,item)
                if action=='rename':
                    name=request.get('name')
                    if not isinstance(name,str) or not 1<=len(name)<=100:raise ValueError('Process name must be 1–100 characters')
                    item['record']['name']=name;self.emit('process',item['record']);return {'process':deepcopy(item['record'])}
                if action=='transfer':
                    if item['retiring'] or item['record']['lifetime']=='temporary' or item['record']['status'] in {'closed','failed'}:raise ValueError('Only a live persistent process can transfer')
                    item['record']['owner']=request['new_owner'];item['record']['owner_epoch']=str(request.get('new_owner_epoch',0))
                    for e in self.executions.values():
                        if e['record']['process_id']==item['record']['id'] and e['record']['status'] not in TERMINAL:
                            e['record']['owner']=request['new_owner'];self.emit('execution',e['record'])
                    self.emit('process',item['record']);return {'process':deepcopy(item['record'])}
            elif action in {'status','cancel','write'}:
                e=self.executions.get(request.get('execution_id'))
                if not e or e['record']['kind']!=kind:raise ValueError('Unknown execution ID/type')
                if action=='status':return {'execution':deepcopy(e['record'])}
                if not actor.get('admin') and actor['id'] not in {e['record']['submitted_by'],self.processes[e['record']['process_id']]['record']['owner']}:raise ValueError('Only submitter, Owner or Master may control execution')
                if action=='cancel':return {'execution':self.cancel(e,'Cancellation requested by '+actor['id'])}
                item=self.processes[e['record']['process_id']]
            else:raise ValueError('Unknown Process Pool operation')
        if action=='create':return {'process':deepcopy(self.create(kind,request,actor)['record'])}
        if action=='submit':return self.submit(kind,request,actor)
        if action=='delete':self.delete(item,'Deleted by '+actor['id']);return {'process':deepcopy(item['record'])}
        if action=='write':return {**item['driver'].write(request.get('text',''),request.get('close_stdin',False)),'execution':deepcopy(e['record'])}

    def close(self):
        with self.lock:self.closed=True;items=list(self.processes.values())
        for item in items:self.delete(item,'Session NodeRuntime closed')
        self.halt.set()
        if self.monitor is not threading.current_thread():self.monitor.join(timeout=2)
