// Focused identity metadata flow, using only temporary Sessions and offline Demo.
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {execFileSync}=require('node:child_process');
const {testDirectory, project,environment,startServer,launchBrowser,cleanup,report}=require('./support.cjs');
(async()=>{
  const temp=testDirectory('recoding-icons-');
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
store=SessionStore.create(Path(sys.argv[1]),'Icon fixture',CONFIG)
master=SeedRuntime(store,dict(CONFIG))
team=MultiAgentSystem(store,dict(CONFIG),lambda s,c,*_:SeedRuntime(s,c),master.publish,master)
team.request('master','set_agent_icon',{'agent_id':'master','icon':'🧠'})
child=team.request('master','create_agent',{'name':'Illustrator','icon':'🔬'})['agent_id']
team.request('master','send_user_message',{'message':'Master report'})
team.request(child,'send_user_message',{'message':'Illustrator report'})
team.request('master','send_agent_message',{'agent_id':child,'message':'Peer instruction'})
team.request('master','set_agent_icon',{'agent_id':child,'icon':'🎨'})
team.request('master','set_agent_icon',{'agent_id':'master','icon':'🦉'})
team.pause()
print(json.dumps({'id':store.id,'child':child}))
`,temp],{cwd:project,env:environment({PYTHONPATH:path.join(project,'tests')}),encoding:'utf8'}));
    server=startServer(temp);const base=await server.ready;browser=await launchBrowser();
    const page=await browser.newPage({viewport:{width:1280,height:850}});page.on('pageerror',e=>errors.push(e.message));
    await page.goto(base,{waitUntil:'domcontentloaded'});await page.waitForFunction(()=>workspaceSync.loaded&&state.config);
    assert(await page.evaluate(id=>openSession({action:'resume',id}),seed.id));
    await page.waitForFunction(()=>state.session?.status==='sleeping'&&!state.switching);
    const icon=(scope,id)=>page.locator(`${scope} [data-agent-icon="${id}"]`).first();
    assert.equal(await icon('#agent-tabs','master').innerText(),'🦉');
    assert.equal(await icon('#agent-list',seed.child).innerText(),'🎨');
    assert.equal(await icon('.human-message','master').innerText(),'🦉','Historical replay must use the current icon');
    await page.locator(`#agent-list [data-agent-id="${seed.child}"]`).click();
    await page.waitForFunction(id=>state.agentId===id,seed.child);
    assert.equal(await icon('#agent-tabs',seed.child).innerText(),'🎨');
    assert.equal(await icon('.human-message',seed.child).innerText(),'🎨');
    await page.locator('#agent-tabs [data-agent-id=all]').click();
    await page.waitForFunction(()=>state.agentId==='all'&&allView().loaded);
    assert.equal(await icon('.all-agent-message.human-message',seed.child).innerText(),'🎨');
    await page.locator('#message-recipient').click();
    assert.equal(await page.locator(`#message-recipients [data-recipient="${seed.child}"]`).count(),0,'Stopped Agents are absent from @ choices');
    await page.keyboard.press('Escape');
    await page.evaluate(id=>dispatch({type:'agents_updated',sequence:state.sequence+1,timestamp:Date.now()/1000,
      agents:state.session.agents.map(agent=>agent.id===id ? {...agent,icon:'👩🏽‍💻',status:'sleeping'} : agent),
      master_icon:'♾️',team:state.session.team}),seed.child);
    assert.equal(await icon('#agent-tabs',seed.child).innerText(),'👩🏽‍💻');
    assert.equal(await icon('#agent-tabs','master').innerText(),'♾️');
    assert.equal(await icon('#agent-list',seed.child).innerText(),'👩🏽‍💻');
    assert(await page.evaluate(id=>[...document.querySelectorAll(`#messages [data-agent-icon="${id}"]`)].every(n=>n.textContent==='👩🏽‍💻'),seed.child));
    await page.locator('#message-recipient').click();
    assert.equal(await icon('#message-recipients',seed.child).innerText(),'👩🏽‍💻');
    await page.locator(`[data-recipient="${seed.child}"]`).click();
    assert.equal(await icon('#message-recipient',seed.child).innerText(),'👩🏽‍💻');
    await page.reload({waitUntil:'domcontentloaded'});
    await page.waitForFunction(()=>state.session?.master_icon==='🦉'&&!state.switching);
    assert.equal(await icon('#agent-list',seed.child).innerText(),'🎨');
    assert.deepEqual(errors,[]);
    result={passed:true,checks:['Persisted Master/sub icons survive replay, selection and reload',
      'Tabs, Agent list, human/peer messages, aggregate messages and @ recipients share icons',
      'Live metadata updates refresh cached headers and emoji sequences render intact'],external_model_api_calls:0,full_suite_run:false};
  }finally{await cleanup({browser,server,temp});}
  report(result);
})().catch(error=>{console.error(error);process.exitCode=1;});
