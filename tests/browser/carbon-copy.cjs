// Historical read-only CC fixture. New send tools must never accept CC.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {execFileSync}=require('node:child_process');
const {project,testDirectory,environment,startServer,launchBrowser,cleanup,report}=require('./support.cjs');
(async()=>{
  const root=testDirectory('recoding-cc-');let server,browser,result;const errors=[];
  try{
    const seed=JSON.parse(execFileSync(path.join(project,'.venv/bin/python3'),['-B','-c',String.raw`
import json,sys,time,uuid
from pathlib import Path
from session import SessionStore
from team_fixtures import fixture_team as MultiAgentSystem
from multiagent.messages import copy_images,load_images
from multiagent.worker import format_input_events
from multiagent.multimodal import context_stats
from test_teams import CONFIG,Runtime
class Seed(Runtime):
 def publish(self,event):return super().publish({'timestamp':time.time(),**event})
root=Path(sys.argv[1]);(root/'plot.jpg').write_bytes(Path(sys.argv[2]).read_bytes())
store=SessionStore.create(root,'CC fixture',CONFIG);master=Seed(store,dict(CONFIG))
team=MultiAgentSystem(store,dict(CONFIG),lambda s,c,*_:Seed(s,c),master.publish,master)
a=team.request('master','create_agent',{'name':'Analyst','icon':'🔬'})['agent_id']
b=team.request('master','create_agent',{'name':'Builder','icon':'🛠'})['agent_id']
args={'agent_id':b,'cc':['master'],'message':'## Peer plan\n\n**Check the evidence.**','images':['plot.jpg']}
peer=team.request(a,'send_agent_message',{k:v for k,v in args.items() if k!='cc'})
human=team.request(a,'send_user_message',{'message':'## Human report\n\n**Finished.**'})
# Seed immutable old-style records directly; this is not a live forwarding API.
def old_copy(original,to,paths):
 original.update(to=to,cc=['master'])
 copied={'id':uuid.uuid4().hex,'source':a,'target':'master','content':original['content'],
  'kind':'message','timestamp':time.time(),'delivery':'cc','to':to,'cc':['master'],
  'original_message_ids':[original['id']],
  'images':copy_images(store,load_images(team._store(a),paths,team.config))}
 team.data['messages'].append(copied)
 return {'agent_id':'master','message_id':copied['id'],'images':copied['images']}
original=next(m for m in team.data['messages'] if m['id']==peer['recipients'][0]['message_id'])
peer['cc']=[old_copy(original,[b],['plot.jpg'])]
old_copy([m for m in team.data['messages'] if m['kind']=='human_output'][-1],['user'],[])
team._save()
stats=context_stats([{'role':'system','content':'fixture'}]);run_id='cc-source'
writer=team.agents[a]
writer.publish({'type':'loop_started','run_id':run_id})
writer.publish({'type':'model_start','run_id':run_id,'step':1,**stats})
writer.publish({'type':'model_end','run_id':run_id,'step':1,'elapsed':.1,'usage':{},'calls':[],'tool_calls':1})
writer.publish({'type':'tool_start','run_id':run_id,'step':1,'number':1,'index':1,'count':1,'call_id':'send-peer','name':'send_agent_message','arguments':json.dumps(args),'origin':'model'})
writer.publish({'type':'tool_end','run_id':run_id,'step':1,'call_id':'send-peer','name':'send_agent_message','result':peer,'elapsed':.1})
run_id='cc-master';master.publish({'type':'loop_started','run_id':run_id})
events,_=format_input_events(team.pending('master',[]),step=1,target='master',offset=0,received_at=time.time())
for event in events:master.publish({**event,'run_id':run_id})
master.publish({'type':'model_start','run_id':run_id,'step':1,**stats})
master.publish({'type':'model_end','run_id':run_id,'step':1,'elapsed':.1,'usage':{},'calls':[],'tool_calls':0})
team.pause()
print(json.dumps({'id':store.id,'a':a,'b':b,'human':human['message_id']}))
`,root,path.join(project,'tests/fixtures/small.jpg')],{cwd:project,env:environment({PYTHONPATH:path.join(project,'tests')}),encoding:'utf8'}));
    server=startServer(root);const base=await server.ready;browser=await launchBrowser();
    const page=await browser.newPage({viewport:{width:1500,height:1050}});page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base);await page.waitForFunction(()=>workspaceSync.loaded);
    await page.evaluate(id=>openSession({action:'browse',id}),seed.id);
    await page.waitForFunction(()=>state.agentId==='all'&&allView().loaded);
    await page.locator('#detail-visibility [data-detail-mode=chat]').click();
    assert.equal(await page.locator('.all-agent-message.cc-message').count(),2);
    const copy=page.locator('.all-agent-message.cc-message').filter({hasText:'Peer plan'});
    assert.match(await copy.locator('.conversation-heading').innerText(),/Analyst.*Builder.*抄送给.*Master/s);
    assert.equal(await copy.locator('h2').innerText(),'Peer plan');
    const image=copy.locator('img');assert.equal(await image.count(),1);
    assert.equal((await page.request.get(new URL(await image.getAttribute('src'),base).href)).status(),200);
    await page.locator('#detail-visibility [data-detail-mode=human]').click();
    assert.equal(await copy.isVisible(),false);
    assert.equal(await page.locator('.all-agent-message.human-message:visible').count(),1);
    await page.evaluate(()=>selectAgent('master'));
    await page.waitForFunction(()=>state.agentId==='master');
    assert.equal(await page.locator('.peer-message.cc-message').count(),2);
    assert.equal(await page.locator('.human-message').count(),0,'CC to Master must not become a report from Master to human');
    assert.match(await page.locator('.peer-message.cc-message').filter({hasText:'Human report'}).innerText(),/知情副本/);
    await page.evaluate(()=>selectAgent('all'));await page.waitForFunction(()=>allView().loaded);
    await page.locator('#detail-visibility [data-detail-mode=chat]').click();
    await page.locator('.all-agent-message.cc-message').filter({hasText:'Peer plan'}).locator('.conversation-avatar').click();
    await page.waitForFunction(id=>state.agentId===id,seed.a);
    assert.equal(await page.locator('.peer-message.message-target').count(),1,'CC source link locates the original sender card');
    assert.match(await page.locator('.peer-message.message-target .conversation-heading').innerText(),/Analyst.*Builder.*CC.*Master/s);
    assert.deepEqual(errors,[]);
    result={passed:true,checks:['CC preserves original To plus separate informational recipients',
      'Original and CC remain distinct in All agents; human-only filter excludes copies',
      'Master sees CC as peer chat, with Markdown and server-served images',
      'Sender icon on a copy locates the original sent message'],external_model_api_calls:0,full_suite_run:false,errors};
  }finally{await cleanup({browser,server});}
  report(result);
})().catch(error=>{console.error(error);process.exitCode=1;});
