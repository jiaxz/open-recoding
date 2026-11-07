#!/usr/bin/env python3
"""Explicit, lock-protected Context maintenance; never starts Agent loops/tools."""
import argparse
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import signal
import time
import uuid

from multiagent.context_maintenance import compress_snapshot
from multiagent.context_window import ContextCompactor, ContextBudget, blocks, prefix_hash
from multiagent.tools import CONTEXT_TOOLS
from multiagent.prompts import TOOLS
from multiagent.tools import combine_schemas, tool_schemas
from multiagent.client import Client
from multiagent.model_config import api_key, api_base_url
from multiagent.runtime_build import RUNTIME_REVISION
from multiagent.storage import StoreLease, atomic_json, read_json, protect_directory
from multiagent.storage import MemberStore
from multiagent.tools import agent_tools
from multiagent.events import update_run, update_token_usage
from session import SessionStore
from session.config import load_env_file
from session.compaction import recover_summary_checkpoint
from tools import definitions


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-s','--session',required=True,help='Session archive directory')
    parser.add_argument('--agent-id',default='master')
    parser.add_argument('--env-file',type=Path)
    parser.add_argument('--recover-journal',type=Path,help='Restore successful summaries from this Agent journal')
    parser.add_argument('--live',action='store_true',help='Explicitly permit summary API requests and commit the new projection')
    parser.add_argument('--probe',action='store_true',help='After compression, make one model request without executing its response')
    parser.add_argument('--report-dir',type=Path,required=True,help='Private ignored report/backup directory')
    args=parser.parse_args()
    if not args.live:parser.error('--live is required; this tool sends the selected history to its configured model API')
    report=args.report_dir.resolve();report.mkdir(parents=True,exist_ok=True)
    load_env_file(args.env_file)
    if not api_key():parser.error('No model API key configured')
    signal.signal(signal.SIGINT,lambda *_:(_ for _ in ()).throw(KeyboardInterrupt()))
    root=SessionStore(args.session)
    with ExitStack() as leases:
        leases.enter_context(StoreLease(root.folder,name='host.lock'))
        leases.enter_context(StoreLease(root.folder))
        store=root if args.agent_id=='master' else MemberStore(root,args.agent_id)
        if store is not root:leases.enter_context(StoreLease(store.folder))
        for name in ['state.json','context-window.json','context-checkpoint.json','context-maintenance.json','context-budget.json']:
            source=store.folder/name
            if source.is_file() and not report.joinpath('before-'+name).exists():report.joinpath('before-'+name).write_bytes(source.read_bytes())
        raw_hash=hashlib.sha256((store.folder/'state.json').read_bytes()).hexdigest()
        saved=store.load_state();messages=saved['messages'];config=root.settings();model=config['model']
        installed=[item for item in definitions() if args.agent_id=='master' or not item.get('master_only')]
        injected=[*agent_tools(args.agent_id),*tool_schemas(installed)]
        selected=combine_schemas(TOOLS,CONTEXT_TOOLS,injected)
        operations=read_json(root.folder/'context-maintenance.json') if (root.folder/'context-maintenance.json').exists() else {'version':1,'agents':[]}
        previous=next((item for item in operations['agents'] if item['agent_id']==args.agent_id),None)
        result={'agent_id':args.agent_id,'model':model,'raw_state_sha256':raw_hash}
        if args.recover_journal:
            if not previous:raise ValueError('No original compression snapshot to verify')
            result['recovered']=recover_summary_checkpoint(store,args.recover_journal,previous)
            print(json.dumps({'stage':'checkpoint_recovered',**result['recovered']}),flush=True)
        request_id=uuid.uuid4().hex
        operation={'agent_id':args.agent_id,'name':'Master' if args.agent_id=='master' else store.name,'model':model,
                   'request_id':request_id,'run_id':'context-'+request_id,'step':saved.get('iteration',1),
                   'snapshot_count':len(messages),'snapshot_sha256':prefix_hash(messages,len(messages)),
                   'status':'running','created_at':time.time(),'updated_at':time.time()}
        operations['agents']=[item for item in operations['agents'] if item['agent_id']!=args.agent_id]+[operation]
        atomic_json(root.folder/'context-maintenance.json',operations)
        sequence=max((event['sequence'] for event in store.iter_events()),default=0)
        team_path=root.folder/'agents/index.json'
        protect_directory(team_path.parent)
        team=read_json(team_path) if team_path.exists() else {'version':root.metadata['version'],'agents':[],'messages':[],'master_runs':{}}
        def publish(event):
            nonlocal sequence
            sequence+=1;value={'timestamp':time.time(),**event,'sequence':sequence};store.append_event(value)
            update_run(team.setdefault('master_runs',{}) if args.agent_id=='master' else next(item for item in team['agents'] if item['id']==args.agent_id).setdefault('runs',{}),value)
            update_token_usage(team.setdefault('token_usage',{}).setdefault(args.agent_id,{}),value)
            if event['type'] in {'context_compaction_start','context_compaction_end','context_compaction_error'}:
                print(json.dumps({k:value.get(k) for k in ('type','generation','prefix_count','elapsed','error') if k in value}),flush=True)
            elif event['type']=='context_compaction_transport' and event.get('phase') in {'error','retry_wait'}:
                print(json.dumps({k:event.get(k) for k in ('phase','attempt','error_category','upstream_code','retry_delay_seconds')}),flush=True)
        try:
            finished=compress_snapshot({**config,'runtime_revision':RUNTIME_REVISION,'session_directory':str(store.folder),
                'directory':str(store.workspace),'store_metadata':store.metadata,'tool_schemas':injected,
                'run_id':operation['run_id'],'snapshot_count':len(messages),'snapshot_sha256':operation['snapshot_sha256'],
                'step':operation['step']},on_event=publish)
            memory=finished['memory'];current=store.load_state()['messages'];cut=memory['prefix_count']
            if not blocks(current[:cut]) or prefix_hash(current,cut)!=memory['prefix_sha256']:raise ValueError('Prefix changed; summary not committed')
            atomic_json(store.folder/'context-window.json',memory)
            operation.update(status='succeeded',generation=memory['generation'],context=finished['context'],updated_at=time.time(),failure_count=0)
            result.update(status='succeeded',context=finished['context'],prefix_count=cut,generation=memory['generation'])
            projection_context={**finished['context'],'full_messages':len(current),'compaction_generation':memory['generation'],
                                'compaction_saved_tokens':memory.get('saved_tokens_estimate')}
            if args.probe:
                validation={'id':uuid.uuid4().hex,'run_id':operation['run_id'],'step':operation['step'],
                            'model':model,'purpose':'context_validation'}
                publish({'type':'context_compaction_start',**validation})
                compact=ContextCompactor(store.folder/'context-window.json',None,model=lambda:model)
                projected=compact.view(current);client=Client(api_key(),config.get('base_url',api_base_url()),model,config.get('api_timeout',120),tools=selected,max_retries=3)
                started=time.monotonic()
                try:
                    response=client.complete(projected)
                    budget=ContextBudget(store.folder/'context-budget.json',model=lambda:model,tools=selected,limits=config.get('context_limits'))
                    budget.mark_request(projected,model);budget.record(client.last_usage)
                    result['probe']={'success':True,'elapsed':time.monotonic()-started,'usage':client.last_usage,'tool_calls':len(response.get('tool_calls') or []),'response_executed':False}
                    projection_context['last_input_tokens']=client.last_usage.get('prompt_tokens')
                    publish({'type':'context_compaction_end',**validation,'elapsed':result['probe']['elapsed'],'usage':client.last_usage,
                             'context':projection_context})
                except Exception as error:
                    result['probe']={'success':False,'elapsed':time.monotonic()-started,'error':str(error),'response_executed':False}
                    publish({'type':'context_compaction_error',**validation,'elapsed':result['probe']['elapsed'],'usage':client.last_usage,'error':str(error)})
            operation['context']=projection_context
            publish({'type':'context_projection_updated','context':projection_context,'origin':'maintenance'})
        except BaseException as error:
            operation.update(status='cancelled' if isinstance(error,KeyboardInterrupt) else 'failed',error=str(error),updated_at=time.time())
            result.update(status=operation['status'],error=str(error))
        finally:
            atomic_json(root.folder/'context-maintenance.json',operations)
            atomic_json(root.folder/'agents/index.json',team)
            result['raw_state_unchanged']=raw_hash==hashlib.sha256((store.folder/'state.json').read_bytes()).hexdigest()
            atomic_json(report/'result.json',result)
        print(json.dumps(result,ensure_ascii=False),flush=True)
        return 0 if result.get('status')=='succeeded' and (not args.probe or result.get('probe',{}).get('success')) else 1


if __name__=='__main__':raise SystemExit(main())
