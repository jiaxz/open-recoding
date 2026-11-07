// One focused flow: correspondence-only aggregation, recipients, media and scroll.
const assert=require('node:assert/strict'),fs=require('node:fs'),https=require('node:https'),os=require('node:os'),path=require('node:path');
const {execFileSync}=require('node:child_process');
const {testDirectory, project,environment,startServer,launchBrowser,localTLS,listen,cleanup,report}=require('./support.cjs');
(async()=>{
  const temp=testDirectory('recoding-all-agents-');
  let server,browser,model,result,created=false;const errors=[];
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
root=Path(sys.argv[1]);root.mkdir(exist_ok=True)
picture=Path(sys.argv[2]).read_bytes();(root/'evidence.jpg').write_bytes(picture)
store=SessionStore.create(root,'All agents fixture',CONFIG)
master=SeedRuntime(store,dict(CONFIG))
team=MultiAgentSystem(store,dict(CONFIG),lambda s,c,*_:SeedRuntime(s,c),master.publish,master)
alpha=team.request('master','create_agent',{'name':'Alpha'})['agent_id']
beta=team.request('master','create_agent',{'name':'Beta'})['agent_id']
for i in range(65):
    team.queue_user('master',f'Historical user message {i}. '+'Context for reading. '*25)
    team.request(alpha,'send_user_message',{'message':f'Historical Alpha reply {i}. '+'Evidence. '*30})
fence=chr(96)*3
team.request(alpha,'send_agent_message',{'agent_id':beta,'message':'## Peer evidence\n\n**Verified** facts.\n\n'+fence+'python\nx = 42\n'+fence,'images':['evidence.jpg']})
team.request(beta,'send_user_message',{'message':'## Beta report\n\n![Evidence](evidence.jpg)\n\n$x^2$','images':['evidence.jpg']})
team.pause()
print(json.dumps({'id':store.id,'alpha':alpha,'beta':beta}))
`,temp,path.join(project,'tests/fixtures/small.jpg')],{cwd:project,env:environment({PYTHONPATH:path.join(project,'tests')}),encoding:'utf8'}));
    const {cert,options}=localTLS(temp);
    model=https.createServer(options,(request,response)=>{
      let raw='';request.on('data',chunk=>raw+=chunk);request.on('end',()=>{
        const body=JSON.parse(raw),master=body.messages[0].content.includes('Your role is MASTER;');
        const name=master&&!created ? 'create_agent' : 'sleep',args=name==='create_agent' ? {name:'Active'} : {seconds:300};
        if(name==='create_agent')created=true;
        response.writeHead(200,{'Content-Type':'text/event-stream'});
        response.end('data: '+JSON.stringify({choices:[{delta:{tool_calls:[{index:0,id:'fixture-'+Date.now(),type:'function',function:{name,arguments:JSON.stringify(args)}}]},finish_reason:'tool_calls'}]})+'\n\ndata: [DONE]\n\n');
      });
    });await listen(model);
    server=startServer(temp,{demo:false,env:{GEMINI_API_KEY:'local-test-only',GEMINI_BASE_URL:`https://127.0.0.1:${model.address().port}/v1`,SSL_CERT_FILE:cert}});
    const base=await server.ready;browser=await launchBrowser();
    const page=await browser.newPage({viewport:{width:1400,height:1000}});page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base,{waitUntil:'domcontentloaded'});await page.waitForFunction(()=>workspaceSync.loaded&&state.config);
    const opened=await page.evaluate(id=>openSession({action:'resume',id}),seed.id);assert(opened,await page.locator('#toast').innerText());
    await page.waitForFunction(()=>['sleeping','failed'].includes(state.session?.status)&&!state.switching);
    assert.equal(await page.evaluate(()=>state.session.status),'sleeping',JSON.stringify(await page.evaluate(()=>({error:state.session.error,events:state.session.events?.slice(-3),errors:window.testErrors})))+' '+errors.join(';'));
    await page.waitForFunction(()=>state.session.agents.some(agent=>agent.name==='Active'&&agent.status==='sleeping'));
    const activeId=await page.evaluate(()=>state.session.agents.find(agent=>agent.name==='Active').id);
    assert.deepEqual(await page.locator('#detail-visibility button:visible').allTextContents(),['只看跟我对话','看Agent之间对话','看更多细节']);
    await page.evaluate(()=>{$('conversation-scroll').scrollTop=0;});
    const before=await page.locator('#conversation-scroll').evaluate(node=>node.scrollTop);
    await page.evaluate(()=>renderAgentEvent({type:'human_message',sequence:state.viewSequence+1,source:'master',target:'user',
      message_id:'no-forced-scroll',content:'New reply while reading history. '.repeat(100),timestamp:Date.now()/1000},true));
    await page.waitForFunction(()=>!conversationRefreshPending);
    assert.equal(await page.locator('#conversation-scroll').evaluate(node=>node.scrollTop),before);
    await page.locator('#jump-to-latest').click();
    assert(await page.locator('#conversation-scroll').evaluate(node=>node.scrollHeight-node.scrollTop-node.clientHeight<2));

    await page.locator('#agent-tabs [data-agent-id=all]').click();await page.waitForFunction(()=>state.agentId==='all'&&allView().loaded);
    assert.deepEqual(await page.locator('#detail-visibility button:visible').allTextContents(),['只看跟我对话','看Agent之间对话']);
    assert.equal(await page.locator('.agent-run,.model-call,.tool-call,.model-output,.conversation-detail-control').count(),0);
    const peer=page.locator('.all-agent-message.peer-message').filter({hasText:'Peer evidence'});
    assert.equal(await peer.locator('h2').innerText(),'Peer evidence');assert(await peer.locator('.hljs-number').count()>0);
    const picture=peer.locator('img');assert.equal(await picture.count(),1);
    const imageResponse=await page.request.get(new URL(await picture.getAttribute('src'),base).href);
    assert.equal(imageResponse.status(),200);assert((await imageResponse.body()).equals(fs.readFileSync(path.join(project,'tests/fixtures/small.jpg'))));
    assert.equal(await page.locator('.all-agent-message.human-message').filter({hasText:'Beta report'}).locator('.katex').count(),1);
    await page.locator('#detail-visibility [data-detail-mode=human]').click();assert.equal(await peer.isVisible(),false);
    await page.locator('#detail-visibility [data-detail-mode=chat]').click();assert.equal(await peer.isVisible(),true);
    assert.equal(await page.locator('.earlier-history').count(),1);await page.locator('.earlier-history').click();
    await page.waitForFunction(()=>!allView().more&&!allView().loadingEarlier);
    assert(await page.locator('.all-agent-message').filter({hasText:'Historical user message 0.'}).count()>0);
    assert.match(await page.locator('#message-recipient').innerText(),/@.*Master/s);
    await page.locator('#prompt').fill('MASTER_ONLY');await page.locator('#send').click();
    await page.waitForFunction(()=>[...allView().messages.values()].some(m=>m.content==='MASTER_ONLY'));
    const only=await page.evaluate(()=>[...allView().messages.values()].filter(m=>m.content==='MASTER_ONLY').map(m=>m.target));assert.deepEqual(only,['master']);

    await page.locator('#message-recipient').click();
    assert.equal(await page.locator(`#message-recipients [data-recipient="${seed.alpha}"],#message-recipients [data-recipient="${seed.beta}"]`).count(),0);
    const groupBoxes=await page.locator('.recipient-groups .recipient-option').evaluateAll(nodes=>nodes.map(node=>({top:node.getBoundingClientRect().top,left:node.getBoundingClientRect().left})));
    assert.equal(groupBoxes.length,2);assert(Math.abs(groupBoxes[0].top-groupBoxes[1].top)<2&&groupBoxes[1].left>groupBoxes[0].left,'Broadcast choices are side by side');
    await page.locator(`[data-recipient="${activeId}"]`).click();
    const jpeg=fs.readFileSync(path.join(project,'tests/fixtures/small.jpg'));
    await page.evaluate(base64=>{const data=new DataTransfer();data.items.add(new File([Uint8Array.from(atob(base64),c=>c.charCodeAt(0))],'clipboard.jpg',{type:'image/jpeg'}));
      $('prompt').dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));},jpeg.toString('base64'));
    await page.waitForFunction(()=>currentDraft().images[0]?.status==='ready');
    await page.locator('#prompt').fill('ALPHA_ONLY');await page.locator('#send').click();
    await page.waitForFunction(()=>[...allView().messages.values()].some(m=>m.content==='ALPHA_ONLY'));
    assert.deepEqual(await page.evaluate(()=>[...allView().messages.values()].filter(m=>m.content==='ALPHA_ONLY').map(m=>m.target)),[activeId]);
    const alphaImage=page.locator('.all-agent-message.user-followup').filter({hasText:'ALPHA_ONLY'}).locator('img');
    assert((await alphaImage.getAttribute('src')).includes(`/agents/${activeId}/images/`));

    await page.locator('#message-recipient').click();await page.locator('[data-recipient=all]').click();
    await page.locator('#prompt').fill('EVERYONE');await page.locator('#send').click();
    await page.waitForFunction(()=>[...allView().messages.values()].filter(m=>m.content==='EVERYONE').length===2);
    const broadcast=page.locator('.all-agent-message.user-followup').filter({hasText:'EVERYONE'});
    assert.equal(await broadcast.count(),1);assert.match(await broadcast.innerText(),/@all/);
    assert.equal((await broadcast.getAttribute('data-message-ids')).split(',').length,2);
    assert.deepEqual((await page.evaluate(()=>[...allView().messages.values()].filter(m=>m.content==='EVERYONE').map(m=>m.target))).sort(),['master',activeId].sort());
    await page.locator('#message-recipient').click();await page.locator('[data-recipient=all_except_master]').click();
    await page.locator('#prompt').fill('ONLY_LIVING_SUBS');await page.locator('#send').click();
    await page.waitForFunction(()=>[...allView().messages.values()].some(m=>m.content==='ONLY_LIVING_SUBS'));
    assert.deepEqual(await page.evaluate(()=>[...allView().messages.values()].filter(m=>m.content==='ONLY_LIVING_SUBS').map(m=>m.target)),[activeId]);
    assert.match(await page.locator('.all-agent-message').filter({hasText:'ONLY_LIVING_SUBS'}).innerText(),/@all（除了 master）/);
    assert(await page.evaluate(ids=>state.session.agents.filter(agent=>ids.includes(agent.id)).every(agent=>agent.status==='stopped'),[seed.alpha,seed.beta]),'Broadcast must not resume stopped workers');
    await page.locator('#detail-visibility [data-detail-mode=human]').click();
    await page.locator('#agent-tabs [data-agent-id=master]').click();await page.waitForFunction(()=>state.agentId==='master');
    assert.equal(await page.locator('#detail-visibility button:visible').count(),3);assert.equal(await page.locator('#message-recipient').isVisible(),false);
    await page.locator('#agent-tabs [data-agent-id=all]').click();await page.waitForFunction(()=>state.agentId==='all');
    assert.equal(await page.locator('#detail-visibility [data-detail-mode=human]').getAttribute('aria-pressed'),'true');
    assert.equal(await page.locator('.model-call,.tool-call,.model-output,.conversation-detail-control').count(),0);
    await page.locator('#stop').click();await page.waitForFunction(()=>state.session.status==='stopped'&&state.session.agents.every(agent=>agent.status==='stopped'));
    await page.locator('#message-recipient').click();assert.equal(await page.locator('#message-recipients>.recipient-option').count(),0);
    assert.equal(await page.locator('.recipient-groups button:disabled').count(),2);await page.keyboard.press('Escape');
    await page.locator('#prompt').fill('No living recipients');assert(await page.locator('#send').isDisabled());
    assert.deepEqual(errors,[]);result={passed:true,checks:[
      'New replies preserve the current reading position; Latest remains an explicit action',
      'All agents combines persistent human/peer messages in time order, pages history and renders Markdown/code/math/remote images',
      'Aggregate view has two modes and no loop details; per-Agent views keep three renamed modes and independent drafts/preferences',
      'Only living Agents appear in @; direct clipboard input, @all and @all excluding Master use the correct recipients',
      'Both broadcasts exclude stopped Agents, are side by side, and disable sending when no recipients remain',
      'Fan-out is shown once with all recipients; server message IDs remain distinct'
    ],external_model_api_calls:0,full_suite_run:false};
  }finally{await cleanup({browser,server,model,temp});}
  report(result);
})().catch(error=>{console.error(error);process.exitCode=1;});
