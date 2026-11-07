"""Manual live Session experiment; never imported by the offline test suite.

Run with --live explicitly. Actual observations stay in ignored local reports,
while the dedicated sessions/tests workspaces are removed after resources close.
"""
import argparse
from collections import Counter,defaultdict
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from multiagent.model_config import api_key,default_model
from multiagent.prompts import TEAM_SYSTEM,SUB_SYSTEM
from session import Session
from session.config import load_env_file

PROJECT=Path(__file__).resolve().parents[2]
COMPLETE='GOLD_NEWS_TASK_COMPLETE'
TASK='''收集黄金最新资讯，下载并产生各自摘要。

验收要求：以运行当天为准，优先最近7天，目标至少6条不同资讯、覆盖至少3个独立网站。关注黄金市场、驱动因素和行业动态，中文英文来源均可。注明每条的实际发布日期、来源和原文链接；若7天内可访问来源不足，可以明确说明并适当扩大日期范围，不能把旧资讯称为最新。
下载各条可访问原文的HTML或正文，并分别写中文摘要：主要事实、关键数字/时间、信息出处、与黄金的关系。不能只下载搜索结果页或以搜索摘要冒充原文；打不开的链接如实记录错误并寻找替代，不能编造抓取成功或价格。
把原文和每条摘要放到 artifacts/gold-news/，并生成 index.json 数组，每项包含 title、source、url、published_at、downloaded_path、summary_path、status。另写 overview.md 汇总结果、时间范围、来源与无法核实的部分。文件路径相对 workspace 或用 /workspace 绝对路径均可。
核验文件后，通过 send_user_message 向我报告交付位置、完成数量和限制，并在真正完成的报告中包含 GOLD_NEWS_TASK_COMPLETE；不要在仅汇报计划/尚未结束时使用此标记。任务完成后不要继续采集，等我后续消息。'''


def write_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def intervals_stats(intervals):
    points=[]
    for start,end in intervals:
        if end>start:points.extend([(start,1),(end,-1)])
    active=peak=0;previous=None;overlap=area=0.0
    for stamp,delta in sorted(points,key=lambda item:(item[0],item[1])):
        if previous is not None:
            elapsed=stamp-previous;area+=active*elapsed
            if active>=2:overlap+=elapsed
        active+=delta;peak=max(peak,active);previous=stamp
    return {'peak':peak,'overlap_seconds':round(overlap,3),'sum_seconds':round(area,3)}


def analyze(archive,started,ended,executions):
    events=[];models=[];tools=[];agents={};usage=Counter();failures=[]
    stores=[('master',archive),*((p.name,p) for p in sorted((archive/'agents').glob('*')) if p.is_dir())]
    for agent_id,folder in stores:
        filename=folder/'events.jsonl'
        rows=[]
        if filename.exists():
            with filename.open() as stream:
                for line in stream:
                    if line.strip():rows.append(json.loads(line))
        agents[agent_id]={'events':len(rows)};open_models={};open_tools={}
        for event in rows:
            if event['type']=='agent_event':continue
            events.append({'agent_id':agent_id,**event})
            key=(event.get('run_id'),event.get('step'));stamp=event.get('timestamp',started)
            if event['type']=='model_start':open_models[key]=event
            elif event['type']=='model_end' and key in open_models:
                first=open_models.pop(key);start=first['timestamp']
                models.append({'agent_id':agent_id,'step':event.get('step'),'start':start,'end':start+event.get('elapsed',max(0,stamp-start)),
                    'usage':event.get('usage',{}),'model':event.get('model'),'partial':False})
                for name in ('prompt_tokens','completion_tokens','total_tokens'):
                    value=event.get('usage',{}).get(name)
                    if type(value) is int:usage[name]+=value
            elif event['type']=='tool_start':open_tools[event.get('call_id')]=event
            elif event['type']=='tool_end':
                first=open_tools.pop(event.get('call_id'),None)
                if first:
                    result=event.get('result',{})
                    tools.append({'agent_id':agent_id,'name':event.get('name'),'start':first['timestamp'],'end':stamp,
                        'elapsed':event.get('elapsed'),'error':result.get('error'),'exit_code':result.get('exit_code')})
            elif event['type'] in {'loop_error','session_error'} and event.get('error'):
                failures.append({'agent_id':agent_id,'error':event['error'],'timestamp':stamp})
        for key,first in open_models.items():
            later=[e['timestamp'] for e in rows if e.get('run_id')==key[0] and e['type'] in {'loop_error','loop_stopped'} and e.get('timestamp',0)>=first['timestamp']]
            models.append({'agent_id':agent_id,'step':key[1],'start':first['timestamp'],'end':min([ended,*later]),'partial':True})
    execution_records=[]
    for record in executions:
        start=record.get('started_at') or record.get('created_at',started)
        end=record.get('finished_at') or ended
        execution_records.append({key:record.get(key) for key in ('id','process_id','kind','source','submitted_by','owner','mode','status','exit_code','created_at','started_at','finished_at','error')})
        execution_records[-1].update(start=start,end=end)
    first_task_steps={}
    for event in events:
        if event['type'] in {'user_input_applied','agent_messages'} and event.get('step'):
            first_task_steps.setdefault(event['agent_id'],event['step'])
    task_models=[r for r in models if r['agent_id'] in first_task_steps and r['step']>=first_task_steps[r['agent_id']]]
    spawned=[e['timestamp']-started for e in events if e['type']=='tool_start' and e.get('name')=='create_agent']
    names={key:Counter(t['name'] for t in tools if t['agent_id']==key) for key in agents}
    return {'wall_seconds':round(ended-started,3),'agent_count':len(agents),'subagent_count':max(0,len(agents)-1),
        'model_calls':len(models),'completed_model_calls':sum(not r['partial'] for r in models),'model_concurrency':intervals_stats((r['start'],r['end']) for r in models),
        'task_model_concurrency':intervals_stats((r['start'],r['end']) for r in task_models),
        'first_spawn_seconds':round(min(spawned),3) if spawned else None,
        'execution_concurrency':intervals_stats((r['start'],r['end']) for r in execution_records),
        'execution_modes':dict(Counter(r['mode'] for r in execution_records)),
        'tools_by_agent':{key:dict(value) for key,value in names.items()},'usage':dict(usage),'failures':failures,
        'models':models,'tools':tools,'executions':execution_records,'events':sorted(events,key=lambda e:e.get('timestamp',0))}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',help='perform real model API and internet requests')
    parser.add_argument('--case',required=True)
    parser.add_argument('--report-root',required=True,type=Path)
    parser.add_argument('--seconds',type=float,default=600)
    parser.add_argument('--model')
    args=parser.parse_args()
    if not args.live:parser.error('This manual experiment requires --live; it uses the configured API.')
    load_env_file(PROJECT/'.env')
    if not api_key():raise ValueError('Model API key is not configured')
    test_root=PROJECT/'sessions'/'tests'
    test_root.mkdir(parents=True,exist_ok=True)
    work=test_root/('gold-news-'+uuid.uuid4().hex[:12]);workspace=work/'workspace';workspace.mkdir(parents=True)
    output=args.report_root.resolve()/args.case;output.mkdir(parents=True,exist_ok=False)
    os.environ['RECODING_HOME']=str(work/'home'/'.recoding')
    model=args.model or default_model();session=None;started=time.time();outcome='timeout';reason=None;messages=[]
    write_json(output/'experiment.json',{'case':args.case,'model':model,'max_seconds':args.seconds,'started_at':started,
        'utc_date':datetime.now(timezone.utc).isoformat(),'execution':TASK,
        'master_prompt_sha256':hashlib.sha256(TEAM_SYSTEM.encode()).hexdigest(),'sub_prompt_sha256':hashlib.sha256(SUB_SYSTEM.encode()).hexdigest()})
    (output/'master-role.txt').write_text(TEAM_SYSTEM);(output/'sub-role.txt').write_text(SUB_SYSTEM)
    try:
        session=Session.create(workspace,'gold-news-'+args.case,config={'model':model,'network':True,'gpu':False,'private_files':[str(PROJECT/'.env')]})
        session.submit(TASK);submitted=time.time();last_print=0
        while time.time()-started<args.seconds:
            messages=session.messages(limit=200)['messages']
            completed=next((m for m in messages if m['source']=='master' and m['target']=='user' and COMPLETE in m['content']),None)
            if completed:outcome='completed';reason=completed['content'];break
            if session.master.status=='failed':outcome='master_failed';reason=session.master.error;break
            now=time.time()
            if now-last_print>=15:
                agents=session.team.listing();resources=[]
                for pool in (session.execution.bash_pool,session.python_pool):resources.extend(pool.snapshot()['executions'].values())
                calls=[]
                with session.master.condition:recent=list(session.master.events)[-30:]
                for e in recent:
                    if e['type']=='agent_event':e={**e['event'],'agent_id':e['agent_id']}
                    if e['type'] in {'tool_start','model_start','human_message'}:calls.append({'agent':e.get('agent_id','master'),'event':e['type'],'name':e.get('name'),'step':e.get('step')})
                progress={'elapsed':round(now-started,1),'master':session.master.status,'subs':[{'name':a['name'],'status':a['status']} for a in agents],
                    'executions':dict(Counter(e['status'] for e in resources)),'artifacts':sum(p.is_file() for p in (workspace/'artifacts').rglob('*')),
                    'recent':calls[-5:]}
                with (output/'progress.jsonl').open('a') as stream:stream.write(json.dumps(progress,ensure_ascii=False)+'\n')
                print(json.dumps(progress,ensure_ascii=False),flush=True);last_print=now
            time.sleep(.25)
        ended=time.time()
        write_json(output/'completion.json',{'outcome':outcome,'reason':reason,'ended_at':ended,'wall_seconds':ended-started})
    finally:
        # Archive original evidence BEFORE attempting derived analysis. Never
        # delete the only copy if archiving or analysis encounters an error.
        if session:
            session.close()
            records=[]
            for pool in (session.execution.bash_pool,session.python_pool):records.extend(pool.snapshot()['executions'].values())
            write_json(output/'executions.json',records)
            write_json(output/'messages.json',session.messages(limit=200)['messages'])
            def ignore(folder,names):
                return [name for name in names if name=='__pycache__' or Path(folder)==workspace/'.recoding'/'runtime' and name in {'python-packages','home'}]
            shutil.copytree(workspace,output/'workspace',ignore=ignore)
            try:
                analysis=analyze(session.store.folder,started,locals().get('ended',time.time()),records)
                analysis.update(outcome=outcome,reason=reason,submitted_at=locals().get('submitted'))
                write_json(output/'analysis.json',analysis)
                result={key:analysis[key] for key in ('outcome','wall_seconds','agent_count','subagent_count','model_calls','model_concurrency','execution_concurrency','execution_modes','usage')}
                print('RESULT '+json.dumps(result,ensure_ascii=False),flush=True)
            finally:
                subprocess.run([sys.executable,str(PROJECT/'tests'/'workspace_cleanup.py')],check=True)
        else:
            subprocess.run([sys.executable,str(PROJECT/'tests'/'workspace_cleanup.py')],check=True)

if __name__=='__main__':main()
