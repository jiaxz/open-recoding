// Focused Human-boundary detail expansion; no external model or real Session.
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {testDirectory, startServer,launchBrowser,cleanup,report}=require('./support.cjs');
(async()=>{
  const temp=testDirectory('recoding-local-details-');
  let server,browser,result;const errors=[];
  try{
    server=startServer(temp);const base=await server.ready;browser=await launchBrowser();
    const page=await browser.newPage({viewport:{width:1400,height:900}});page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base,{waitUntil:'domcontentloaded'});await page.locator('#new-session').click();await page.locator('#session-submit').click();
    await page.waitForFunction(()=>state.session?.status==='sleeping'&&!state.switching);
    await page.locator('#stop').click();await page.waitForFunction(()=>state.session.status==='stopped');
    await page.evaluate(()=>{
      const run_id='local-details',peer='a'.repeat(32),emit=event=>dispatch({run_id,timestamp:Date.now()/1000,...event,sequence:state.sequence+1});
      state.session.agents=[{id:peer,name:'Peer',status:'sleeping',created_at:Date.now()/1000}];
      const user=(id,step)=>{
        emit({type:'user_input_queued',message_id:id,target:'master',content:`用户问题 ${id}`,attachments:[]});
        emit({type:'user_input_applied',message_id:id,target:'master',content:`用户问题 ${id}`,attachments:[],step,input_order:0});
      };
      const call=step=>{emit({type:'model_start',step,model:'grok-4.7'});emit({type:'model_end',step,content:`模型说明 ${step}`,tool_calls:1,elapsed:1});};
      const reply=id=>emit({type:'human_message',source:'master',target:'user',message_id:id,images:[],content:`## 回复 ${id}\n\n${'这是一段给用户阅读的证据和结果。'.repeat(80)}`});
      window.localFixture={emit,user,call,reply,peer};emit({type:'loop_start'});user('u1',101);
      emit({type:'agent_messages',step:101,messages:[{id:'peer-one',source:peer,target:'master',content:'Peer 研究进展',images:[],input_order:1}]});
      call(101);emit({type:'tool_start',number:1,name:'python',call_id:'python-1',arguments:'{"code":"print(1)"}'});
      emit({type:'tool_end',number:1,name:'python',call_id:'python-1',elapsed:.1,result:{stdout:'1'}});reply('a1');call(102);reply('a2');user('u2',103);call(103);reply('a3');
    });
    const mode=value=>page.locator(`#detail-visibility button[data-detail-mode=${value}]`);
    const before=id=>page.locator(`.conversation-detail-control[data-detail-for="before/${id}"] button`);
    const after=id=>page.locator(`.conversation-detail-control[data-detail-for="after/${id}"] button`);
    const visible=step=>page.locator(`.model-call[data-step="${step}"]`).isVisible();
    const peer=page.locator('.peer-message[data-message-id=peer-one]');
    await mode('human').click();
    assert.equal(await page.locator('.conversation-detail-control:visible').count(),5);
    assert.equal(await page.locator('.peer-message .conversation-detail-control').count(),0,'Only Human correspondence receives expansion buttons');
    assert.equal(await before('a1').evaluate(node=>node.parentElement.nextElementSibling.dataset.messageId),'a1');
    assert.equal(await after('u1').evaluate(node=>node.parentElement.previousElementSibling.dataset.messageId),'u1');
    await before('a1').click();assert.equal(await visible(101),true);assert.equal(await visible(102),false);assert.equal(await peer.isVisible(),true);
    const firstTop=await peer.evaluate(node=>node.getBoundingClientRect().top-$('conversation-scroll').getBoundingClientRect().top);
    assert(Math.abs(firstTop-12)<4,'Reply-side expansion starts at its relevant process');
    const boundary=()=>before('a1').evaluate(node=>node.getBoundingClientRect().top-$('conversation-scroll').getBoundingClientRect().top);
    await before('a1').evaluate(node=>{$('conversation-scroll').scrollTop+=node.getBoundingClientRect().top-$('conversation-scroll').getBoundingClientRect().top-12;});
    const top=await boundary();await before('a1').click();const closedTop=await boundary();
    assert(Math.abs(closedTop-top)<4,`Closing details preserves the clicked message boundary: ${top} → ${closedTop}`);
    assert.equal(await visible(101),false);assert.equal(await peer.isVisible(),false);
    await before('a2').click();assert.equal(await visible(101),true);assert.equal(await visible(102),true);assert.equal(await visible(103),false);
    await before('a1').click();assert.equal(await visible(101),false);assert.equal(await visible(102),true,'The latest overlapping range choice wins');
    await after('u1').click();assert.equal(await visible(101),true);assert.equal(await visible(102),true);
    await mode('human').click();assert.equal(await visible(101),false);assert.equal(await visible(102),false);assert.equal(await peer.isVisible(),false);
    await mode('chat').click();assert.equal(await peer.isVisible(),true);assert.equal(await visible(101),false);
    await before('a1').click();assert.equal(await visible(101),true);assert.equal(await visible(102),false);
    await before('a1').click();assert.equal(await visible(101),false);assert.equal(await peer.isVisible(),true,'Collapsing extras keeps baseline Agent chat');
    await after('u2').click();
    await page.evaluate(()=>{localFixture.call(104);localFixture.reply('a4');});
    await page.waitForFunction(()=>document.querySelector('.model-call[data-step="104"]').classList.contains('conversation-detail-visible'));
    assert.equal(await visible(103),true);assert.equal(await visible(104),true,'New steps join an expanded ongoing Human-input range');
    await page.evaluate(()=>{localFixture.user('u3',105);localFixture.call(105);});
    assert.equal(await visible(105),false,'A following Human input ends the prior range');
    await page.evaluate(()=>{
      const view=state.views.get('local-details');window.savedLocalSnapshot={...state.session,events:[...state.rootHistory.values()],sequence:state.sequence};
      if(view.inputBatches.get(101).node.nextElementSibling!==view.models.get(101).node)throw new Error('READ input batch moved away from its Model call');
      installChild({...state.session,id:localFixture.peer,name:'Peer',events:[],sequence:0,status:'stopped'},localFixture.peer);
    });
    await mode('human').click();assert.equal(await page.locator('.conversation-detail-control').count(),0);
    await page.evaluate(()=>install(savedLocalSnapshot,{tail:false}));
    assert.equal(await mode('chat').getAttribute('aria-pressed'),'true');assert.equal(await visible(104),true);assert.equal(await visible(105),false);
    assert.equal(await page.locator('.conversation-detail-control:visible').count(),7,'Agent switches and history replay retain ranges without duplicate buttons');
    await mode('chat').click();assert.equal(await visible(104),false);assert.equal(await peer.isVisible(),true);
    await mode('all').click();assert.equal(await page.locator('.conversation-detail-control:visible').count(),0);assert.equal(await visible(105),true);
    assert.deepEqual(errors,[]);result={passed:true,checks:['Human-input after / toHuman before buttons restored, without buttons on Peer chat',
      'Bounded and overlapping ranges work within Human-only and Agent-chat modes; selected-mode clicks clear local expansions',
      'New steps join open ranges; next input ends them; Agent switching and replay preserve state and card chronology',
      'Reply expansion and collapse maintain useful reading positions'],external_model_api_calls:0,full_suite_run:false,errors};
  }finally{await cleanup({browser,server,temp});}
  report(result);
})().catch(error=>{console.error(error);process.exitCode=1;});
