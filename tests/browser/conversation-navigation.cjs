// Focused workspace browsing, stopped input and three-mode presentation flow.
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {execFileSync}=require('node:child_process');
const {testDirectory, startServer,launchBrowser,cleanup,report,project,environment}=require('./support.cjs');
(async()=>{
  const temp=testDirectory('recoding-conversation-');
  let server,browser,result;const errors=[];
  try {
    const ordinary=path.join(temp,'ordinary'),child=path.join(ordinary,'child'),known=path.join(temp,'projects','known');
    fs.mkdirSync(child,{recursive:true});fs.mkdirSync(path.join(temp,'node_modules','package'),{recursive:true});
    execFileSync('python3',['-c','import sys; from session.store import SessionStore; SessionStore.create(sys.argv[1], "known", {})',known],{cwd:project,env:environment()});
    server=startServer(temp);const base=await server.ready;
    browser=await launchBrowser();const page=await browser.newPage({viewport:{width:1400,height:1000}});
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base,{waitUntil:'domcontentloaded'});
    const row=directory=>page.locator(`details[data-directory="${directory}"]`);
    await row(known).waitFor({state:'attached'});
    assert.equal(await row(path.join(temp,'projects')).getAttribute('open'),'','Required ancestors expand to reveal their Sessions');
    assert.equal(await row(known).getAttribute('open'),'');
    const compact=await row(path.join(temp,'projects')).locator(':scope > summary').evaluate(node=>({weight:getComputedStyle(node).fontWeight,height:node.getBoundingClientRect().height,margin:getComputedStyle(node.parentElement).marginBottom}));
    assert.equal(compact.weight,'400');assert(compact.height<=24);assert.equal(compact.margin,'0px');
    await row(path.join(temp,'projects')).locator(':scope > summary').click();
    assert.equal(await row(ordinary).getAttribute('open'),null);
    assert.equal(await row(path.join(temp,'node_modules')).getAttribute('open'),null);
    assert.equal(await row(child).count(),0,'Ordinary descendants are requested only when expanded');
    await row(ordinary).locator(':scope > summary').click();await row(child).waitFor();
    await row(child).locator(':scope > summary > .workspace-add').click();
    assert.equal(await page.locator('#session-directory').inputValue(),child);
    await page.locator('#session-name').fill('Conversation checks');await page.locator('#session-submit').click();
    await page.waitForFunction(()=>state.session?.status==='sleeping'&&!state.switching);
    assert.equal(await page.evaluate(()=>state.session.directory),child);
    await page.reload({waitUntil:'domcontentloaded'});
    await page.waitForFunction(()=>state.session?.status==='sleeping'&&!state.switching&&workspaceSync.loaded);
    assert.equal(await row(ordinary).getAttribute('open'),'','Startup reveals the path to the resumed Session');
    fs.mkdirSync(path.join(ordinary,'new-folder'));
    await row(path.join(ordinary,'new-folder')).waitFor();
    fs.rmdirSync(path.join(ordinary,'new-folder'));
    await row(path.join(ordinary,'new-folder')).waitFor({state:'detached'});
    await page.locator('#stop').click();await page.waitForFunction(()=>state.session.status==='stopped');
    await page.evaluate(base64=>{
      const data=new DataTransfer();data.items.add(new File([Uint8Array.from(atob(base64),c=>c.charCodeAt(0))],'queued.jpg',{type:'image/jpeg'}));
      document.querySelector('#prompt').dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));
    },fs.readFileSync(path.join(__dirname,'../fixtures/small.jpg')).toString('base64'));
    await page.waitForFunction(()=>currentDraft().images[0]?.status==='ready');
    await page.locator('#prompt').fill('STOPPED_HUMAN_NOTE');assert.equal(await page.locator('#send').isEnabled(),true);
    await page.locator('#send').click();await page.locator('.user-followup[data-status=queued]').filter({hasText:'STOPPED_HUMAN_NOTE'}).waitFor();
    const queued=await page.evaluate(async()=>{
      const id=[...state.eventLog.values()].find(event=>event.type==='user_input_queued'&&event.content==='STOPPED_HUMAN_NOTE').message_id;
      const snapshot=await api(`/api/sessions/${state.session.id}`);
      return {id,status:snapshot.status,applied:snapshot.events.some(event=>event.type==='user_input_applied'&&event.message_id===id),
        root:state.userInputs.get(id).node.parentElement.id};
    });
    assert.equal(queued.status,'stopped');assert.equal(queued.applied,false);assert.equal(queued.root,'messages');
    await page.locator('#continue-task').click();
    await page.waitForFunction(id=>state.session.status==='sleeping'&&state.userInputs.get(id)?.node.dataset.status==='applied',queued.id);
    assert.equal(await page.locator('#agent-limit').getAttribute('type'),'text');
    await page.locator('#toggle-agent-settings').click();
    await page.locator('#agent-limit').focus();const limit=await page.locator('#agent-limit').inputValue();
    await page.keyboard.press('ArrowUp');assert.equal(await page.locator('#agent-limit').inputValue(),limit);
    await page.locator('#agent-limit').fill('8');await page.locator('#agent-limit').press('Tab');
    await page.waitForFunction(()=>state.session.team.max_subagents===8);
    await page.evaluate(()=>install({...state.session,events:[],model_rates:[],sequence:state.sequence},{tail:false}));
    await page.evaluate(()=>{
      const now=Date.now()/1000,run_id='dialog-fixture';
      const emit=event=>dispatch({timestamp:now,run_id,...event,sequence:state.sequence+1});
      const user=(id,content,step)=>{
        emit({type:'user_input_queued',message_id:id,source:'user',target:'master',content,attachments:[]});
        emit({type:'user_input_applied',message_id:id,source:'user',target:'master',content,attachments:[],step,input_order:0});
      };
      const call=(step,content)=>{
        emit({type:'model_start',step,model:'gemini-4-argon'});
        emit({type:'model_end',step,model:'gemini-4-argon',elapsed:1,content,tool_calls:0,usage:{prompt_tokens:100,completion_tokens:20}});
      };
      const reply=(id,content)=>emit({type:'human_message',message_id:id,source:'master',target:'user',content,images:[]});
      emit({type:'loop_start'});user('u1','用户的第一个问题',101);call(101,'DETAIL_A');
      emit({type:'tool_start',number:1,name:'python',arguments:'{"code":"print(1)"}'});
      emit({type:'tool_end',number:1,name:'python',result:{stdout:'1'},elapsed:0.01});
      reply('a1','## 第一份回复\n\n**图文内容**，公式 $x^2$。');
      call(102,'DETAIL_B');reply('a2','第二份回复');
      user('u2','用户的第二个问题',103);call(103,'DETAIL_C');reply('a3','第三份回复');
    });
    const mode=value=>page.locator(`#detail-visibility button[data-detail-mode=${value}]`);
    await mode('human').click();
    const reply=id=>page.locator(`.human-message[data-message-id="${id}"]`);
    const visible=step=>page.evaluate(step=>state.views.get('dialog-fixture').models.get(step).node.checkVisibility(),step);
    assert.equal(await page.locator('.human-message:visible').count(),3);assert.equal(await page.locator('.user-followup:visible').count(),2);
    const design=await reply('a1').evaluate(node=>({background:getComputedStyle(node).backgroundColor,gradient:getComputedStyle(node).backgroundImage,
      routes:node.querySelectorAll('.message-route,.coordination-category').length,heading:node.querySelector('.conversation-heading').innerText}));
    assert.equal(design.background,'rgb(0, 0, 0)');assert.equal(design.gradient,'none');assert.equal(design.routes,0);
    assert(!/MESSAGE TO YOU|TO YOU|SEND|RECEIVE/.test(design.heading));assert.equal(await reply('a1').locator('.katex').count(),1);
    assert.equal(await visible(101),false);await mode('chat').click();assert.equal(await visible(101),false);
    await mode('all').click();assert.equal(await visible(103),true);
    assert.equal(await page.locator('.conversation-detail-control:visible').count(),0);
    assert.equal(await page.evaluate(()=>{const view=state.views.get('dialog-fixture');return view.inputBatches.get(101).node.nextElementSibling===view.models.get(101).node;}),true);
    const line=await page.locator('.speed-session-line').evaluate(node=>({dash:getComputedStyle(node).strokeDasharray,width:Number.parseFloat(getComputedStyle(node).strokeWidth),opacity:Number(getComputedStyle(node).strokeOpacity),bottom:node.parentElement.firstElementChild===node}));
    assert.equal(line.dash,'none');assert(line.width<1);assert(line.opacity<=.55);assert.equal(line.bottom,true);
    assert(Number(await page.locator('.speed-line').first().evaluate(node=>getComputedStyle(node).strokeOpacity))<=.6);
    await page.evaluate(()=>install({...state.session,events:[...state.rootHistory.values()],sequence:state.sequence,model_rates:[...modelRates.values()]},{tail:false}));
    assert.equal(await visible(102),true);assert.equal(await visible(103),true);
    await mode('human').click();assert.equal(await visible(101),false);await mode('all').click();
    await page.evaluate(()=>{
      const run_id='dialog-fixture',emit=event=>dispatch({timestamp:Date.now()/1000,run_id,...event,sequence:state.sequence+1});
      emit({type:'model_start',step:104,model:'gemini-4-argon'});
      emit({type:'user_input_queued',message_id:'u3',source:'user',target:'master',content:'模型生成途中的新消息',attachments:[]});
      emit({type:'model_end',step:104,model:'gemini-4-argon',elapsed:1,content:'INFLIGHT_OUTPUT',tool_calls:0,usage:{completion_tokens:20}});
      emit({type:'human_message',message_id:'a4',source:'master',target:'user',content:'读取新消息前，上一调用刚刚返回。',images:[]});
    });
    assert.equal(await page.evaluate(()=>{
      const output=state.views.get('dialog-fixture').models.get(104).responseDetail,user=state.userInputs.get('u3').node;
      return output.checkVisibility()&&Boolean(user.compareDocumentPosition(output)&Node.DOCUMENT_POSITION_FOLLOWING);
    }),true,'An in-flight response belongs after the input that arrived before it, in its chronological position');
    assert.deepEqual(errors,[]);
    result={passed:true,checks:['Compact, regular-weight directory rows; Session directories and necessary ancestors expand, including after page reload',
      'External directory creation/deletion synchronizes while preserving expansion',
      'Stopped Master accepts clipboard images and user text without resuming; the queued message is read after explicit Resume',
      'Concurrency uses a plain numeric text field and saves without spinner arrows',
      'Human correspondence has independent conversational headers and pure-black Agent replies with Markdown/math',
      'Three display modes preserve correspondence, model-input order and history replay',
      'Both speed curves are muted; Session sum remains a thin solid line underneath call curves'],external_model_api_calls:0,errors,full_suite_run:false};
  } finally {await cleanup({browser,server,temp});}
  report(result);
})().catch(error=>{console.error(error);process.exitCode=1;});
