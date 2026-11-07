// Current three-mode conversation and Session-discovery regression; offline only.
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {execFileSync}=require('node:child_process');
const {testDirectory, startServer,launchBrowser,cleanup,report,project,environment}=require('./support.cjs');
(async()=>{
  const temp=testDirectory('recoding-agent-chat-');
  let server,browser,result;const errors=[];
  const create=(directory,name)=>JSON.parse(execFileSync('python3',['-c',
    'import sys,json; from session.store import SessionStore; s=SessionStore.create(sys.argv[1],sys.argv[2],{}); print(json.dumps({"folder":str(s.folder),"id":s.id}))',
    directory,name],{cwd:project,env:environment(),encoding:'utf8'}));
  try {
    const ordinary=path.join(temp,'ordinary'),parent=path.join(temp,'projects'),nested=path.join(parent,'middle','work');
    fs.mkdirSync(ordinary,{recursive:true});fs.mkdirSync(nested,{recursive:true});
    server=startServer(temp);const base=await server.ready;
    browser=await launchBrowser();const page=await browser.newPage({viewport:{width:1450,height:1000}});
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base,{waitUntil:'domcontentloaded'});
    const folder=directory=>page.locator(`.workspace-group[data-directory="${directory}"]`);
    await folder(parent).waitFor();
    assert.equal(await folder(parent).getAttribute('open'),null);
    assert.equal(await folder(ordinary).getAttribute('open'),null);
    const first=create(nested,'discovered');
    await page.locator(`.session-row[data-id="${first.id}"]`).waitFor();
    for(const directory of [parent,path.join(parent,'middle'),nested])assert.equal(await folder(directory).getAttribute('open'),'','New Sessions reveal their entire path');
    assert.equal(await folder(ordinary).getAttribute('open'),null,'Unrelated directories stay collapsed');
    await folder(parent).locator(':scope > summary').click();await page.evaluate(()=>listSessions());
    assert.equal(await folder(parent).getAttribute('open'),null,'Unchanged polls preserve a deliberate collapse');
    const second=create(nested,'new-discovery');
    await page.locator(`.session-row[data-id="${second.id}"]`).waitFor();
    assert.equal(await folder(parent).getAttribute('open'),'','A newly discovered Session overrides the previous collapse');
    await page.locator(`.session-row[data-id="${first.id}"] .session-item`).click();
    await page.waitForFunction(()=>state.session?.status==='sleeping'&&!state.switching);
    await page.reload({waitUntil:'domcontentloaded'});
    await page.waitForFunction(()=>state.session?.status==='sleeping'&&!state.switching);
    assert.equal(await folder(parent).getAttribute('open'),'');assert.equal(await folder(ordinary).getAttribute('open'),null);
    assert.equal(await page.locator('.inspector-heading,#connection,#hide-inspector').count(),0);
    assert.equal(await page.locator('#toggle-inspector').count(),1,'Topbar still controls the right pane');
    assert.equal(await page.locator('#idle-sleep').getAttribute('type'),'text');
    if(!await page.locator('#agent-settings-panel').isVisible())await page.locator('#toggle-agent-settings').click();
    await page.locator('#idle-sleep').fill('3.5');await page.locator('#idle-sleep').press('Tab');
    await page.waitForFunction(()=>state.session.team.idle_sleep_seconds===3.5);
    await page.locator('#idle-sleep').focus();await page.keyboard.press('ArrowUp');assert.equal(await page.locator('#idle-sleep').inputValue(),'3.5');
    await page.locator('#idle-sleep').fill('0.05');await page.locator('#idle-sleep').press('Tab');
    assert.equal(await page.locator('#idle-sleep').evaluate(node=>node.checkValidity()),false);
    await page.locator('#idle-sleep').fill('3.5');await page.locator('#idle-sleep').press('Tab');
    await page.locator('#stop').click();await page.waitForFunction(()=>state.session.status==='stopped');
    await page.evaluate(()=>{
      const run_id='chat-order',peer='b'.repeat(32),now=Date.now()/1000;
      const emit=event=>dispatch({run_id,timestamp:now,...event,sequence:state.sequence+1});
      state.session.agents=[{id:peer,name:'Research peer',status:'sleeping',created_at:now}];
      const content='## Evidence\n\n**A useful result**: $x^2$.\n\n```python\ndef solve(x):\n    return x + 1\n```';
      window.chatFixture={emit,peer,content,run_id};
      emit({type:'loop_start'});emit({type:'model_start',step:101,model:'grok-4.7'});
      emit({type:'model_end',step:101,content:'INTERNAL_MODEL_OUTPUT',tool_calls:1,elapsed:1,usage:{completion_tokens:20}});
      emit({type:'tool_start',step:101,number:1,call_id:'send-one',name:'send_agent_message',arguments:JSON.stringify({agent_id:peer,message:content})});
      emit({type:'user_input_queued',message_id:'during-send',target:'master',content:'USER_DURING_SEND',attachments:[]});
      emit({type:'tool_end',step:101,number:1,call_id:'send-one',name:'send_agent_message',elapsed:.1,result:{recipients:[{agent_id:peer,message_id:'sent-one'}],images:[]}});
      const messages=[{id:'incoming-one',source:peer,target:'master',timestamp:now,content,images:[],input_order:0}];
      emit({type:'agent_messages',step:102,messages});
      emit({type:'model_start',step:102,model:'grok-4.7'});
      emit({type:'model_end',step:102,content:'INTERNAL_AFTER_REPLY',tool_calls:0,elapsed:1});
      emit({type:'human_message',message_id:'human-reply',source:'master',target:'user',content:'## For you\n\nA separate human reply.',images:[]});
    });
    const outgoing=page.locator('.peer-message[data-direction=outgoing]'),incoming=page.locator('.peer-message[data-message-id=incoming-one]');
    for(const chat of [outgoing,incoming]) {
      assert.equal(await chat.locator('h2').innerText(),'Evidence');assert.equal(await chat.locator('.katex').count(),1);
      assert(await chat.locator('code .hljs-keyword').count()>0);assert.match(await chat.locator('.message-route').innerText(),/Master[\s\S]*Research peer|Research peer[\s\S]*Master/);
      assert.equal(await chat.locator('.coordination-category,.conversation-detail-control').count(),0);
      const style=await chat.evaluate(node=>({bg:getComputedStyle(node).backgroundColor,image:getComputedStyle(node).backgroundImage}));
      assert(style.bg.startsWith('rgb('));assert.equal(style.image,'none');
    }
    assert.match(await outgoing.locator('.peer-role').innerText(),/^me/);assert.match(await incoming.locator('.peer-role').innerText(),/^others/);
    const chronology=()=>page.evaluate(()=>{
      const before=(a,b)=>Boolean(a.compareDocumentPosition(b)&Node.DOCUMENT_POSITION_FOLLOWING),view=state.views.get('chat-order');
      const user=state.userInputs.get('during-send').node,out=view.steps.querySelector('.peer-message[data-direction=outgoing]'),inc=view.steps.querySelector('.peer-message[data-direction=incoming]');
      const batch=view.inputBatches.get(102).node,model=view.models.get(102).node,reply=view.steps.querySelector('.human-message');
      return before(user,out)&&before(out,inc)&&before(inc,batch)&&batch.nextElementSibling===model&&before(model,reply);
    });
    assert(await chronology());
    const mode=value=>page.locator(`#detail-visibility button[data-detail-mode=${value}]`);
    await mode('human').click();assert.equal(await outgoing.isVisible(),false);assert.equal(await incoming.isVisible(),false);
    assert.equal(await page.locator('.human-message[data-message-id=human-reply]').isVisible(),true);
    await mode('chat').click();assert.equal(await outgoing.isVisible(),true);assert.equal(await incoming.isVisible(),true);
    assert.equal(await page.locator('.model-call:visible,.model-output:visible,.tool-call:visible,.model-inputs:visible,.system-message:visible,.run-heading:visible').count(),0);
    assert.equal(await page.locator('.conversation-detail-control:visible').count(),2);assert(await chronology());
    await mode('all').click();assert.equal(await page.locator('.peer-input-receipt').isVisible(),true);
    assert.equal(await page.locator('.model-call[data-step="102"]').isVisible(),true);assert(await chronology());
    await mode('chat').click();
    await page.evaluate(()=>{
      window.masterChatSnapshot={...state.session,events:[...state.rootHistory.values()],sequence:state.sequence};
      installChild({...state.session,id:chatFixture.peer,name:'Research peer',events:[],sequence:0,status:'sleeping'},chatFixture.peer);
    });
    assert.equal(await mode('chat').getAttribute('aria-pressed'),'true');await mode('human').click();
    await page.evaluate(()=>install(masterChatSnapshot,{tail:false}));assert.equal(await mode('chat').getAttribute('aria-pressed'),'true');
    assert.equal(await page.locator('.peer-message').count(),2,'Replaying a page does not duplicate conversations');
    for(const width of [700,390,320]) {
      await page.setViewportSize({width,height:900});await page.waitForFunction(()=>document.documentElement.scrollWidth<=innerWidth);
      const contained=await page.locator('#detail-visibility').evaluate(node=>node.getBoundingClientRect().right<=innerWidth&&node.scrollWidth<=node.clientWidth+1);
      assert(contained,`Three-mode control fits ${width}px`);
    }
    assert.deepEqual(errors,[]);
    result={passed:true,checks:['Polling/reload expands newly discovered Session paths; ordinary branches remain closed; unchanged polls respect manual collapse',
      'Removed inspector header; decimal idle-sleep input has no stepper and validates its range',
      'Peer me/others chat uses formatted, solid conversation bubbles and preserves send/read/model chronology',
      'Three display modes isolate per-Agent preferences, replay without duplicates, and fit narrow viewports'],external_model_api_calls:0,full_suite_run:false,errors};
  }finally{await cleanup({browser,server,temp});}
  report(result);
})().catch(error=>{console.error(error);process.exitCode=1;});
