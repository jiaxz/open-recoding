// Offline All agents display filters: AND/OR, human override, routing and preferences.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {execFileSync}=require('node:child_process');
const {project,testDirectory,environment,startServer,launchBrowser,cleanup,report}=require('./support.cjs');
(async()=>{
  const root=testDirectory('recoding-message-filters-');let server,browser,result;const errors=[];
  try{
    const seed=JSON.parse(execFileSync(path.join(project,'.venv/bin/python3'),['-B','-c',String.raw`
import json,sys,time
from pathlib import Path
from session import SessionStore
from team_fixtures import fixture_team as MultiAgentSystem
from multiagent.multimodal import context_stats
from test_teams import CONFIG,Runtime
class Seed(Runtime):
 def publish(self,event):return super().publish({'timestamp':time.time(),**event})
root=Path(sys.argv[1])
store=SessionStore.create(root,'Filter fixture',CONFIG);master=Seed(store,dict(CONFIG))
team=MultiAgentSystem(store,dict(CONFIG),lambda s,c,*_:Seed(s,c),master.publish,master)
a=team.request('master','create_agent',{'name':'Alpha','icon':'🔬'})['agent_id']
b=team.request('master','create_agent',{'name':'Beta','icon':'🛠'})['agent_id']
c=team.request('master','create_agent',{'name':'Gamma','icon':'🎨'})['agent_id']
d=team.request('master','create_agent',{'name':'Archived','icon':'📚'})['agent_id']
for target in ['master',a,b]:team.queue_user(target,'own-'+target)
def peer(source,target,body,**extra):
 return team.request(source,'send_agent_message',{'agent_id':target,'message':body,**extra})
peer(a,b,'A to B');peer(a,c,'A to C');peer(c,b,'C to B');peer('master',a,'Master to A')
team.request(a,'send_agent_message',{'agent_ids':[b,c],'message':'A group to B and C'})
peer(a,b,'A second note to B');peer(a,'master','A to Master')
team.request(a,'send_user_message',{'message':'## A report\n\n**Human report.**'})
team.request(b,'send_user_message',{'message':'B report'})
team.request(d,'send_user_message',{'message':'Old report'})
run_id='observer-fixture'
master.publish({'type':'loop_started','run_id':run_id})
master.publish({'type':'model_start','run_id':run_id,'step':1,**context_stats([{'role':'system','content':'fixture'}])})
master.publish({'type':'model_end','run_id':run_id,'step':1,'elapsed':.1,'usage':{},'calls':[],'tool_calls':2})
for number,(name,fields) in enumerate([('read_global_messages',{'limit':2}),('listen_global_messages',{'seconds':0})],1):
 master.publish({'type':'tool_start','run_id':run_id,'step':1,'number':number,'index':number,'count':2,'call_id':name,'name':name,'arguments':json.dumps(fields),'origin':'model'})
 extra={'seen':[m['id'] for m in team.data['messages'] if m['target']=='master']} if name=='listen_global_messages' else {}
 result=team.request('master',name,{**fields,**extra})
 master.publish({'type':'tool_end','run_id':run_id,'step':1,'call_id':name,'name':name,'result':result,'elapsed':.01})
team.pause()
second=SessionStore.create(root,'Other filter fixture',CONFIG)
second_master=Seed(second,dict(CONFIG))
other=MultiAgentSystem(second,dict(CONFIG),lambda s,c,*_:Seed(s,c),second_master.publish,second_master)
other.request('master','send_user_message',{'message':'Other report'});other.pause()
print(json.dumps({'id':store.id,'second':second.id,'a':a,'b':b,'c':c,'d':d,'index':str(team.path)}))
`,root],{cwd:project,env:environment({PYTHONPATH:path.join(project,'tests')}),encoding:'utf8'}));
    const originalIndex=fs.readFileSync(seed.index,'utf8');
    server=startServer(root);const base=await server.ready;browser=await launchBrowser();
    const page=await browser.newPage({viewport:{width:1450,height:1050}});page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base);await page.waitForFunction(()=>workspaceSync.loaded);
    // UI metadata fixture only: archives remain stopped/read-only on the server.
    const live=()=>page.evaluate(stopped=>{
      state.session.status='running';
      for(const agent of state.session.agents)agent.status=agent.id===stopped ? 'stopped' : 'running';
      inspector();renderAllMessages();
    },seed.d);
    const open=async id=>{
      await page.evaluate(id=>openSession({action:'browse',id}),id);
      await page.waitForFunction(id=>state.session?.id===id&&state.agentId==='all'&&allView().loaded,id);
      await live();
    };
    const rows=()=>page.locator('.all-agent-message:visible').evaluateAll(cards=>cards.map(card=>({
      source:card.dataset.source,targets:card.dataset.target.split(','),copy:card.classList.contains('cc-message'),
      body:card.querySelector('.final-response').textContent.trim(),
    })));
    const filterPanel=page.locator('#all-message-filter-panel');
    const field=name=>filterPanel.locator(`[data-filter-field=${name}]`);
    const showFilter=async()=>{if(!await filterPanel.isVisible())await page.locator('#all-message-filter').click();};
    const choose=async(name,id,checked=true,only=false)=>{
      await showFilter();
      if(only)await field(name).locator('[data-action=clear]').click();
      await field(name).locator(`input[data-person-id="${id}"]`).setChecked(checked);
      await page.keyboard.press('Escape');
    };
    const reset=async()=>{await showFilter();await page.locator('#reset-message-filters').click();await page.keyboard.press('Escape');};
    const own=cards=>assert.equal(cards.filter(card=>card.source==='user').length,3,'Human messages bypass both filters');
    await open(seed.id);
    assert.deepEqual(await page.locator('#detail-visibility button:visible').allTextContents(),['只看跟我对话','看部分agent对话','看全部agent对话']);
    const total=(await rows()).length;assert.equal(total,13);
    assert.equal(await page.locator('.all-agent-message .message-reply').count(),total,'Every message has a conversation action');
    await showFilter();
    for(const name of ['senders','recipients']){
      assert.equal(await field(name).locator('input[data-person-id]:checked').count(),5,'All checks every listed person, including me');
      assert.equal(await field(name).locator(`input[data-person-id="${seed.d}"]`).count(),0,'Stopped Agent is not a filter candidate');
    }
    await field('senders').locator(`input[data-person-id="${seed.a}"]`).uncheck();
    assert.ok((await rows()).every(card=>card.source!==seed.a),'Unchecking from all excludes only that sender');
    await field('senders').locator('[data-action=all]').check();
    assert.equal((await rows()).length,total);await page.keyboard.press('Escape');
    await choose('senders',seed.a,true,true);
    let cards=await rows();own(cards);assert.equal(cards.length,9);
    assert.ok(cards.every(card=>['user',seed.a].includes(card.source)));
    await choose('recipients',seed.b,true,true);
    cards=await rows();own(cards);assert.equal(cards.length,6);
    assert.ok(cards.filter(card=>card.source!=='user').every(card=>card.source===seed.a&&card.targets.includes(seed.b)&&!card.copy));
    assert.deepEqual(cards.find(card=>card.body==='A group to B and C').targets.sort(),[seed.b,seed.c].sort(),'A filter keeps complete broadcast routing');
    await choose('senders',seed.c);
    cards=await rows();own(cards);assert.equal(cards.length,7,'Multiple selected senders are OR');
    await choose('recipients','user');
    cards=await rows();assert.equal(cards.length,8,'Multiple selected recipients are OR');
    await choose('recipients',seed.b,false);
    cards=await rows();own(cards);assert.equal(cards.length,4);assert.match(cards.at(-1).body,/A report\s*Human report\./);
    assert.equal(await page.locator('.all-agent-message:visible h2').innerText(),'A report','Filtered reports keep Markdown');
    await reset();
    await choose('recipients','master',true,true);
    cards=await rows();own(cards);assert.equal(cards.length,4);
    assert.deepEqual(cards.filter(card=>card.source!== 'user')[0].targets,['master'],'Filtering matches the actual recipient');
    await showFilter();
    const humanCheckbox=field('senders').locator('input[data-person-id=user]');
    assert.equal(await humanCheckbox.isChecked(),true);assert.equal(await humanCheckbox.isDisabled(),true);
    await field('senders').locator('[data-action=clear]').click();
    await page.keyboard.press('Escape');
    cards=await rows();own(cards);assert.equal(cards.length,3);
    assert.equal(await page.locator('#all-message-filter').evaluate(node=>node===document.activeElement),true,'Escape closes the picker and restores focus');
    await page.locator('#detail-visibility [data-detail-mode=chat]').click();
    assert.equal((await rows()).length,total,'Ordinary conversation mode ignores the saved custom filter');
    await showFilter();await page.keyboard.press('Escape');assert.equal((await rows()).length,3);
    await page.evaluate(()=>selectAgent('master'));
    assert.equal(await page.locator('#all-message-filter').isVisible(),false);
    assert.deepEqual(await page.locator('#detail-visibility button:visible').allTextContents(),['只看跟我对话','看Agent之间对话','看更多细节']);
    await page.locator('#detail-visibility [data-detail-mode=all]').click();
    assert.equal(await page.locator('.coordination-call[data-action=read_global_messages] .coordination-category').innerText(),'GLOBAL MESSAGES');
    assert.match(await page.locator('.coordination-call[data-action=listen_global_messages] .coordination-brief').innerText(),/全局监听.*timeout/);
    await page.evaluate(()=>selectAgent('all'));assert.equal((await rows()).length,3);
    await open(seed.second);
    assert.equal(await page.evaluate(()=>detailMode),'chat','Another Session has independent display settings');
    await choose('recipients','master',true,true);
    assert.equal((await rows()).length,0);
    assert.match(await page.locator('.all-messages-empty').innerText(),/没有匹配/);
    await open(seed.id);assert.equal((await rows()).length,3,'Returning restores this Session filters');
    await page.reload();await page.waitForFunction(()=>state.agentId==='all'&&allView().loaded);
    await live();
    assert.equal((await rows()).length,3,'Filters persist across page reloads');
    await reset();assert.equal((await rows()).length,total);
    const reply=body=>page.locator('.all-agent-message').filter({has:page.locator('.final-response').filter({hasText:body})}).locator('.message-reply');
    assert.equal(await page.locator('#prompt').isDisabled(),true,'Browsing remains read-only');
    // A draft saved while previously running survives read-only navigation.
    await page.evaluate(()=>{currentDraft().text='Keep this draft';$('prompt').value=currentDraft().text;});
    await reply('A report').click();
    assert.equal(await page.evaluate(()=>composerRecipient()),seed.a);
    assert.equal(await page.locator('#prompt').isDisabled(),true,'Reply cannot unlock a stopped archive');
    assert.equal(await page.locator('#prompt').inputValue(),'Keep this draft');
    await reply('B report').click();assert.equal(await page.evaluate(()=>composerRecipient()),seed.b);
    await reply('own-master').click();assert.equal(await page.evaluate(()=>composerRecipient()),'master');
    await reply('Old report').click();
    await page.waitForFunction(id=>state.agentId===id,seed.d);
    assert.equal(await page.locator('#prompt').isDisabled(),true,'A stopped sender opens read-only without waking it');
    await page.evaluate(()=>selectAgent('all'));await live();
    assert.equal(await page.locator('#prompt').inputValue(),'Keep this draft','Reply navigation keeps All agents draft');
    await page.setViewportSize({width:320,height:850});await showFilter();
    const bounds=await filterPanel.boundingBox();assert.ok(bounds.x>=0&&bounds.x+bounds.width<=320,`Filter panel fits narrow screens: ${JSON.stringify(bounds)}`);
    assert.equal(fs.readFileSync(seed.index,'utf8'),originalIndex,'Filtering and browsing must not consume or rewrite inboxes');
    assert.deepEqual(errors,[]);
    result={passed:true,checks:['Requested mode labels/order; second mode opens filters; ordinary modes bypass conditions',
      'All checks every listed person; only living Agents are offered',
      'Sender/recipient multi-select: OR within each field, AND between them',
      'Human-originated messages always visible, including empty selections',
      'Broadcast routes preserved; actual recipient matching',
      'Markdown preserved, empty-state hint and reset',
      'Per-Session preferences persist across agent switches and reloads',
      'Reply selects sender, preserves drafts and respects read-only input without waking stopped Agents',
      'Global observation tools appear as explicit detail cards, not delivered messages',
      'Escape closes the panel; mobile layout fits; stopped archive and unread queues unchanged'],external_model_api_calls:0,full_suite_run:false,errors};
  }finally{await cleanup({browser,server});}
  report(result);
})().catch(error=>{console.error(error);process.exitCode=1;});
