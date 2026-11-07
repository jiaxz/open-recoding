// Focused presentation regression: chronological messages and per-Agent composer settings.
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {testDirectory, startServer,launchBrowser,cleanup,report}=require('./support.cjs');
(async()=>{
  const temp=testDirectory('recoding-message-scope-');
  let server,browser,result;const errors=[];
  try {
    server=startServer(temp);const base=await server.ready;
    browser=await launchBrowser();const page=await browser.newPage({viewport:{width:1300,height:900}});
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base,{waitUntil:'domcontentloaded'});
    await page.locator('#new-session').click();await page.locator('#session-name').fill('Message scope');await page.locator('#session-submit').click();
    await page.waitForFunction(()=>state.session?.status==='sleeping'&&!state.switching);
    assert.equal(await page.locator('#agent-limit').inputValue(),'6');
    // Journal fixtures exercise dispatch, history replay and page switching;
    // the actual server/Session/renderer run unchanged in offline Demo mode.
    await page.evaluate(()=>{
      const child='c'.repeat(32),now=Date.now()/1000;
      state.session.agents=[{id:child,name:'Scope peer',status:'sleeping',created_at:now}];
      function journal(event){dispatch({...event,sequence:state.sequence+1,timestamp:now});}
      journal({type:'loop_start',run_id:'timeline-fixture'});
      journal({type:'model_start',run_id:'timeline-fixture',step:101,model:'gemini-4-argon'});
      journal({type:'user_input_queued',run_id:'timeline-fixture',message_id:'human-one',target:'master',content:'HUMAN_ANCHORED_MESSAGE',attachments:[]});
      const original=state.userInputs.get('human-one').node;
      function input(source,id,content,sequence=1){
        const event={type:'human_message',source,target:'user',message_id:id,content,images:[],timestamp:now+sequence,sequence};
        dispatch(source==='master' ? {...event,sequence:state.sequence+1} : {type:'agent_event',agent_id:source,event,sequence:state.sequence+1});
        return event;
      }
      window.scopeFixture={child,input,childEvent:input(child,'child-one','SUB_ONLY_MESSAGE'),now};
      input('master','master-one','MASTER_ONLY_MESSAGE');
      journal({type:'user_input_applied',run_id:'timeline-fixture',message_id:'human-one',target:'master',content:'HUMAN_ANCHORED_MESSAGE',attachments:[],step:102,input_order:0});
      journal({type:'model_start',run_id:'timeline-fixture',step:102,model:'gemini-4-argon'});
      scopeFixture.original=original;
    });
    const timeline=await page.evaluate(()=>{
      const original=state.userInputs.get('human-one').node,receipt=document.querySelector('.user-input-receipt'),view=state.views.get('timeline-fixture');
      const before=(a,b)=>Boolean(a.compareDocumentPosition(b)&Node.DOCUMENT_POSITION_FOLLOWING);
      const human=document.querySelector('.human-message'),batch=receipt.parentElement,model=view.models.get(102).node;
      const incoming=getComputedStyle(original),outgoing=getComputedStyle(human);
      return {same:original===scopeFixture.original,anchored:before(original,human)&&before(human,batch),adjacent:batch.nextElementSibling===model,
        originalInBatch:batch.contains(original),solid:incoming.backgroundImage==='none'&&outgoing.backgroundImage==='none',
        different:incoming.backgroundColor!==outgoing.backgroundColor,opaque:[incoming.backgroundColor,outgoing.backgroundColor].every(color=>color.startsWith('rgb('))};
    });
    assert.deepEqual(timeline,{same:true,anchored:true,adjacent:true,originalInBatch:false,solid:true,different:true,opaque:true});
    assert.equal(await page.locator('.human-message').count(),1);
    assert.match(await page.locator('.human-message').innerText(),/MASTER_ONLY_MESSAGE/);
    assert.equal(await page.evaluate(()=>unreadCount(state.session.id,scopeFixture.child)),1);
    await page.evaluate(()=>install({...state.session,events:[...state.rootHistory.values()],sequence:state.sequence},{tail:false}));
    assert.equal(await page.locator('.human-message').filter({hasText:'SUB_ONLY_MESSAGE'}).count(),0);
    assert.equal(await page.evaluate(()=>unreadCount(state.session.id,scopeFixture.child)),1,'Master replay must not read a sub message');
    assert.equal(await page.locator('.user-followup').count(),1);assert.equal(await page.locator('.user-input-receipt').count(),1);
    await page.locator('#prompt').fill('MASTER_DRAFT');
    await page.locator('#detail-visibility button[data-detail-mode=human]').click();
    await page.locator('#composer-resizer').focus();await page.keyboard.press('ArrowUp');
    const masterHeight=await page.locator('#prompt').evaluate(node=>node.style.maxHeight);
    assert.equal(await page.locator('#toggle-composer').count(),0,'Input stays visible without a collapse control');
    await page.evaluate(()=>{currentDraft().submitting=true;controls();});
    assert.equal(await page.locator('.user-followup').isVisible(),true);assert.equal(await page.locator('.human-message').isVisible(),true);
    assert.equal(await page.locator('.user-input-receipt').isVisible(),false);
    await page.evaluate(()=>{
      installChild({...state.session,id:scopeFixture.child,name:'Scope peer',events:[scopeFixture.childEvent],sequence:1,status:'sleeping'},scopeFixture.child);
    });
    assert.equal(await page.locator('.human-message').count(),1);
    assert.match(await page.locator('.human-message').innerText(),/SUB_ONLY_MESSAGE/);
    assert.equal(await page.evaluate(()=>unreadCount(state.session.id,scopeFixture.child)),0);
    assert.equal(await page.locator('#detail-visibility button[data-detail-mode=human]').getAttribute('aria-pressed'),'false');
    assert.equal(await page.locator('#composer-body').isVisible(),true);
    assert.equal(await page.locator('#prompt').evaluate(node=>node.style.maxHeight),'160px');
    assert.equal(await page.locator('#prompt').inputValue(),'');await page.locator('#prompt').fill('SUB_DRAFT');
    assert.equal(await page.locator('#send').isEnabled(),true,'Another Agent sending must not disable the selected Agent composer');
    await page.evaluate(()=>scopeFixture.input('master','master-two','MASTER_LIVE_MESSAGE',2));
    assert.equal(await page.locator('.human-message').filter({hasText:'MASTER_LIVE_MESSAGE'}).count(),0);
    assert.equal(await page.evaluate(()=>unreadCount(state.session.id,'master')),1);
    await page.evaluate(()=>install({...state.session,events:[...state.rootHistory.values()],sequence:state.sequence},{tail:false}));
    assert.equal(await page.locator('.human-message').count(),2);
    assert.equal(await page.locator('.human-message').filter({hasText:'SUB_ONLY_MESSAGE'}).count(),0);
    assert.equal(await page.evaluate(()=>unreadCount(state.session.id,'master')),0);
    assert.equal(await page.locator('#detail-visibility button[data-detail-mode=human]').getAttribute('aria-pressed'),'true');
    assert.equal(await page.locator('#composer-body').isVisible(),true);
    assert.equal(await page.locator('#prompt').evaluate(node=>node.style.maxHeight),masterHeight);
    assert.equal(await page.locator('#prompt').inputValue(),'MASTER_DRAFT');
    assert.equal(await page.locator('#send').isEnabled(),false);await page.evaluate(()=>{currentDraft().submitting=false;controls();});
    await page.evaluate(()=>{
      const key=composerKey();composerPreferences.delete(key);loadComposerPreferences();
    });
    assert.equal(await page.locator('#detail-visibility button[data-detail-mode=human]').getAttribute('aria-pressed'),'true','Current-Agent settings must survive storage reload');
    await page.evaluate(()=>scopeFixture.input(scopeFixture.child,'child-two','SUB_LIVE_MESSAGE',2));
    assert.equal(await page.locator('.human-message').filter({hasText:'SUB_LIVE_MESSAGE'}).count(),0);
    assert.equal(await page.evaluate(()=>unreadCount(state.session.id,scopeFixture.child)),1);
    assert.equal(await page.locator('.human-message:visible').count(),2);
    await page.evaluate(()=>{
      const secondSession={...state.session,id:'d'.repeat(32),events:[],sequence:0};install(secondSession,{tail:false});
    });
    assert.equal(await page.locator('#detail-visibility button[data-detail-mode=human]').getAttribute('aria-pressed'),'false','Settings must not leak into another Session');
    assert.equal(await page.locator('#composer-body').isVisible(),true);assert.equal(await page.locator('#prompt').inputValue(),'');
    assert.deepEqual(errors,[]);
    result={passed:true,checks:['Default sub-agent concurrency is 6','Sent messages stay anchored; separate read receipts precede the consuming Model call','Human directions use distinct, opaque dark backgrounds without gradients','Per-Agent messages and unread status remain isolated on live updates and replay','Composer remains visible; drafts, size and detail settings persist per Session/Agent'],external_model_api_calls:0,errors,full_suite_run:false};
  } finally {await cleanup({browser,server,temp});}
  report(result);
})().catch(error=>{console.error(error);process.exitCode=1;});
