// Focused defaults, cache accounting display and message notification navigation.
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {testDirectory, startServer,launchBrowser,cleanup,report}=require('./support.cjs');
(async()=>{
  const temp=testDirectory('recoding-preferences-');
  let server,browser,result;const errors=[];
  try {
    server=startServer(temp,{env:{GEMINI_MODEL:'gemini-4-argon'}});const base=await server.ready;
    browser=await launchBrowser();const page=await browser.newPage({viewport:{width:1400,height:950}});
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base,{waitUntil:'domcontentloaded'});await page.waitForFunction(()=>state.config&&workspaceSync.loaded);
    await page.locator('#new-session').click();await page.locator('#session-submit').click();
    await page.waitForFunction(()=>state.session?.status==='sleeping'&&!state.switching);
    const mode=value=>page.locator(`#detail-visibility button[data-detail-mode=${value}]`);
    assert.equal(await mode('chat').getAttribute('aria-pressed'),'true');
    assert.equal(await page.locator('.model-call:visible').count(),0);
    await page.locator('#stop').click();await page.waitForFunction(()=>state.session.status==='stopped');
    // Render the production creation form with the server's real default;
    // keep the actual worker offline throughout this test.
    await page.evaluate(()=>{state.config.demo=false;state.config.model='grok-4.7';newSession();});
    assert.equal(await page.locator('#session-model').inputValue(),'grok-4.7');
    await page.locator('#dismiss-dialog').click();await page.evaluate(()=>state.config.demo=true);
    await page.evaluate(()=>{
      const run_id='preferences',now=Date.now()/1000,peer='b'.repeat(32);
      window.preferenceFixture={peer,emit:(event,live=true)=>dispatch({run_id,timestamp:now,...event,sequence:state.sequence+1},live)};
      state.session.agents=[{id:peer,name:'Research colleague',created_at:now,status:'stopped',resumable:true}];
      const emit=preferenceFixture.emit;
      emit({type:'loop_start'});
      emit({type:'user_input_queued',message_id:'user-note',target:'master',content:'Human instruction',attachments:[]});
      emit({type:'agent_messages',step:101,messages:[{id:'peer-note',source:peer,target:'master',timestamp:now,content:'## Peer finding\n\n**A useful result.**',images:[],input_order:0}]});
      emit({type:'model_start',step:101,model:'grok-4.7'});
      state.session.team={...state.session.team,prompt_tokens:250,completion_tokens:30,cached_tokens:115,uncached_tokens:65,
        cache_known_input_tokens:180,cache_unknown_input_tokens:70};inspector();
    });
    const incoming=page.locator('.peer-message[data-message-id=peer-note]'),user=page.locator('.user-followup[data-message-id=user-note]');
    const bg=node=>node.evaluate(value=>({color:getComputedStyle(value).backgroundColor,image:getComputedStyle(value).backgroundImage}));
    const [peerStyle,userStyle]=await Promise.all([bg(incoming),bg(user)]);
    assert.notEqual(peerStyle.color,userStyle.color);assert.equal(peerStyle.image,'none');assert.equal(userStyle.image,'none');
    assert.equal(await incoming.locator('h2').innerText(),'Peer finding');
    assert.equal(await page.locator('.team-token-total[data-metric=prompt_tokens] strong').innerText(),'250');
    assert.match(await page.locator('.team-cache-hit').innerText(),/115 · 63\.9%/);
    assert.equal(await page.locator('.team-cache-miss').innerText(),'已知 Non-cache 65');
    assert.match(await page.locator('.team-cache-unknown').innerText(),/70 input/);
    await page.evaluate(()=>{Object.assign(state.session.team,{prompt_tokens:180,cache_unknown_input_tokens:0});inspector();});
    assert.equal(await page.locator('.team-cache-miss').innerText(),'Non-cache 65');
    assert.equal(await page.locator('.team-cache-unknown').count(),0);
    const sessionId=await page.evaluate(()=>state.session.id),peer=await page.evaluate(()=>preferenceFixture.peer);
    const target={type:'human_message',run_id:'peer-history',sequence:40,timestamp:Date.now()/1000,source:peer,target:'user',
      message_id:'notification-target',content:'## Found it\n\n**The requested evidence.**',images:[]};
    const later=Array.from({length:35},(_,index)=>({...target,sequence:41+index,message_id:`later-${index}`,
      content:`Later reply ${index}\n\n${'Additional findings. '.repeat(30)}`}));
    let historyRequests=0;
    await page.route(`**/api/sessions/${sessionId}/agents/${peer}`,route=>route.fulfill({json:{id:peer,name:'Research colleague',
      status:'stopped',resumable:true,model:'grok-4.7',events:later,sequence:75,history_truncated:true}}));
    await page.route(`**/api/sessions/${sessionId}/agents/${peer}/history?*`,route=>{
      historyRequests++;return route.fulfill({json:{events:[{type:'loop_start',run_id:'peer-history',sequence:1,timestamp:target.timestamp},target],has_more:false}});
    });
    await page.locator('#prompt').fill('Draft in progress');await page.locator('#prompt').focus();
    await page.evaluate(({target,peer})=>preferenceFixture.emit({type:'agent_event',agent_id:peer,event:target}),{target,peer});
    assert.match(await page.locator('#toast').innerText(),/Research colleague 给你发来消息/);
    const checkNoticePlacement=async()=>{
      const placement=await page.evaluate(()=>{
        const notice=$('toast').getBoundingClientRect(),tabs=$('agent-navigation').getBoundingClientRect(),
          pane=$('conversation-scroll').getBoundingClientRect(),input=$('prompt').getBoundingClientRect();
        return {underTabs:notice.top>=tabs.bottom,beforeConversation:notice.bottom<=pane.top,
          inputUncovered:document.elementFromPoint(input.x+input.width/2,input.y+input.height/2)===$('prompt'),
          withinColumn:notice.left>=tabs.left&&notice.right<=tabs.right,
          focused:document.activeElement===$('prompt')};
      });
      assert.deepEqual(placement,{underTabs:true,beforeConversation:true,inputUncovered:true,withinColumn:true,focused:true});
    };
    await checkNoticePlacement();await page.keyboard.type(' still typing');
    assert.equal(await page.locator('#prompt').inputValue(),'Draft in progress still typing');
    await page.setViewportSize({width:390,height:950});
    await page.waitForFunction(()=>$("session-sidebar").getBoundingClientRect().right<=0);await checkNoticePlacement();
    await page.setViewportSize({width:1400,height:950});
    await page.locator('#toast .toast-action').click();
    const found=page.locator('.human-message[data-message-id=notification-target]');
    await found.waitFor();assert.equal(await mode('chat').getAttribute('aria-pressed'),'true','New Agent views default to chat');
    assert.equal(historyRequests,1,'Jump loads older history when the target is outside the preview');
    assert.equal(await page.evaluate(()=>state.agentId),peer);assert.equal(await found.locator('h2').innerText(),'Found it');
    assert(await found.evaluate(node=>document.activeElement===node));
    const located=await found.evaluate(node=>{
      const box=node.getBoundingClientRect(),pane=$('conversation-scroll').getBoundingClientRect();
      return box.top>=pane.top&&box.bottom<=pane.bottom&&$('conversation-scroll').scrollHeight-$('conversation-scroll').scrollTop-$('conversation-scroll').clientHeight>500;
    });assert(located,'The destination message is visible without jumping to the latest message');
    // Keyboard activation on a notification in the current Agent also targets
    // its exact message, even after newer replies have arrived.
    await page.evaluate(()=>{
      const target={type:'human_message',run_id:'peer-history',source:state.agentId,target:'user',message_id:'keyboard-target',
        content:'Keyboard target',images:[],sequence:state.viewSequence+1,timestamp:Date.now()/1000};
      renderAgentEvent(target,true);
      for(let i=0;i<20;i++)renderAgentEvent({...target,message_id:`tail-${i}`,content:'More content. '.repeat(50),sequence:state.viewSequence+1},false);
    });
    await page.locator('#toast .toast-action').focus();await page.keyboard.press('Enter');
    assert.equal(await page.evaluate(()=>document.activeElement?.dataset.messageId),'keyboard-target');
    assert.equal(await page.locator('#toast').isVisible(),false);
    await mode('all').click();await page.evaluate(()=>loadComposerPreferences());
    assert.equal(await mode('all').getAttribute('aria-pressed'),'true','An explicit display preference stays saved');
    assert.deepEqual(errors,[]);result={passed:true,checks:[
      'New Session/model selector defaults to Grok; Agent display defaults to chat and retains explicit preferences',
      'Incoming peer chats and user messages use different solid backgrounds',
      'All Agents shows cache hits, known non-cache input and unknown coverage without treating missing as zero',
      'New-message notices sit below Agent tabs without covering or interrupting typing on desktop and narrow screens',
      'Mouse/keyboard notification activation selects the source Agent, loads older history and focuses the exact formatted message'
    ],external_model_api_calls:0,full_suite_run:false,errors};
  }finally{await cleanup({browser,server,temp});}
  report(result);
})().catch(error=>{console.error(error);process.exitCode=1;});
