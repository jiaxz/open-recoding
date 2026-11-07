// Sender-icon navigation: exact messages, paged history and grouped peer sends.
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {execFileSync}=require('node:child_process');
const {testDirectory, project,environment,startServer,launchBrowser,cleanup,report}=require('./support.cjs');
(async()=>{
  const temp=testDirectory('recoding-message-source-');
  let server,browser,result;const errors=[];
  try{
    const seed=JSON.parse(execFileSync('python3',['-B','-c',String.raw`
import json,sys,time
from pathlib import Path
from session.store import SessionStore
from team_fixtures import fixture_team as MultiAgentSystem
from test_teams import CONFIG,Runtime
class SeedRuntime(Runtime):
    def publish(self,event):
        return super().publish({'timestamp':time.time(),**event})
store=SessionStore.create(Path(sys.argv[1]),'Source navigation fixture',CONFIG)
master=SeedRuntime(store,dict(CONFIG))
team=MultiAgentSystem(store,dict(CONFIG),lambda s,c,*_:SeedRuntime(s,c),master.publish,master)
sender=team.request('master','create_agent',{'name':'Sender','icon':'📚'})['agent_id']
peer=team.request('master','create_agent',{'name':'Peer','icon':'🔬'})['agent_id']
early=team.request(sender,'send_user_message',{'message':'## EARLY_REPORT\n\nA report from earlier history.'})['message_id']
runtime=team.agents[sender]
args={'agent_ids':['master',peer],'message':'## GROUP_FINDING\n\n**One original send**, multiple recipient IDs.'}
runtime.publish({'type':'tool_start','run_id':runtime.run_id,'step':1,'number':1,'call_id':'group-send','name':'send_agent_message','arguments':json.dumps(args)})
sent=team.request(sender,'send_agent_message',args)
runtime.publish({'type':'tool_end','run_id':runtime.run_id,'step':1,'number':1,'call_id':'group-send','name':'send_agent_message','elapsed':0.01,'result':sent})
for i in range(805):
    runtime.publish({'type':'test_padding'})
for i in range(12):
    team.request(sender,'send_user_message',{'message':f'Latest report {i}. '+'Reading space. '*80})
team.pause()
print(json.dumps({'id':store.id,'sender':sender,'early':early,'peerIds':[item['message_id'] for item in sent['recipients']]}))
`,temp],{cwd:project,env:environment({PYTHONPATH:path.join(project,'tests')}),encoding:'utf8'}));
    server=startServer(temp);const base=await server.ready;browser=await launchBrowser();
    const page=await browser.newPage({viewport:{width:1280,height:850}});page.on('pageerror',e=>errors.push(e.message));
    let historyRequests=0;page.on('request',request=>{if(request.url().includes(`/agents/${seed.sender}/history?`))historyRequests++;});
    await page.goto(base,{waitUntil:'domcontentloaded'});await page.waitForFunction(()=>workspaceSync.loaded&&state.config);
    assert(await page.evaluate(id=>openSession({action:'browse',id}),seed.id));
    await page.waitForFunction(()=>state.session?.read_only&&!state.switching);
    await page.locator(`#agent-list [data-agent-id="${seed.sender}"]`).click();
    await page.waitForFunction(id=>state.agentId===id,seed.sender);
    await page.locator('#detail-visibility [data-detail-mode=human]').click();
    const openAll=async()=>{
      await page.locator('#agent-tabs [data-agent-id=all]').click();
      await page.waitForFunction(()=>state.agentId==='all'&&allView().loaded);
    };
    const assertCentered=async card=>assert(await card.evaluate(node=>{
      const box=node.getBoundingClientRect(),frame=document.getElementById('conversation-scroll').getBoundingClientRect();
      return box.top>=frame.top-1&&box.top<frame.bottom&&document.activeElement===node;
    }),'The exact message remains focused in the conversation viewport');
    await openAll();
    await page.locator(`.all-agent-message[data-message-id="${seed.early}"] .message-sender-link`).click();
    await page.waitForFunction(id=>state.agentId===id&&Boolean(document.querySelector('.human-message.message-target')),seed.sender);
    const early=page.locator(`.human-message[data-message-id="${seed.early}"]`);
    assert(await early.isVisible());assert(historyRequests>0,'Older sender history is loaded on demand');
    assert.equal(await page.locator(`#agent-tabs [data-agent-id="${seed.sender}"]`).getAttribute('aria-selected'),'true');
    await assertCentered(early);await page.waitForTimeout(300);await assertCentered(early);
    await openAll();
    // The icon inside the sender's name follows the same exact-message route.
    await page.locator('.all-agent-message.peer-message .message-sender .agent-icon').click();
    await page.waitForFunction(id=>state.agentId===id&&Boolean(document.querySelector('.peer-message.message-target')),seed.sender);
    const sent=page.locator('.peer-message.message-target');
    assert.match(await sent.innerText(),/GROUP_FINDING/);assert(await sent.isVisible());
    assert.deepEqual((await sent.getAttribute('data-message-ids')).split(','),seed.peerIds);
    assert.equal(await page.evaluate(()=>detailMode),'human','Only the target is revealed; the saved display mode stays intact');
    await assertCentered(sent);
    await page.locator('#detail-visibility [data-detail-mode=human]').click();assert.equal(await sent.isVisible(),false);
    await openAll();
    let release,arrived,first=true;
    const held=new Promise(resolve=>release=resolve),pending=new Promise(resolve=>arrived=resolve);
    const endpoint=`**/api/sessions/${seed.id}/agents/${seed.sender}`;
    await page.route(endpoint,async route=>{if(first){first=false;arrived();await held;}await route.continue();});
    await page.locator(`.all-agent-message[data-message-id="${seed.early}"] .message-sender-link`).click();await pending;
    // A later ordinary selection of the same Agent must cancel the old jump.
    await page.locator(`#agent-list [data-agent-id="${seed.sender}"]`).click();
    await page.waitForFunction(id=>state.agentId===id,seed.sender);release();
    await page.waitForTimeout(300);
    assert.equal(await page.locator('.message-target').count(),0,'A stale source jump cannot override a later tab selection');
    await page.unroute(endpoint);
    assert.deepEqual(errors,[]);
    result={passed:true,checks:['Avatar and inline sender icons select the sender tab and focus the exact message',
      'Older history loads on demand; grouped sends match every recipient ID',
      'Hidden peer targets reveal without changing display preferences or jumping to the latest message',
      'A later tab selection cancels an earlier delayed message jump'],external_model_api_calls:0,full_suite_run:false};
  }finally{await cleanup({browser,server,temp});}
  report(result);
})().catch(error=>{console.error(error);process.exitCode=1;});
