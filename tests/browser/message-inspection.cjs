// Focused rich-detail / compact-composer check. Offline Demo, temporary data only.
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {testDirectory, startServer,launchBrowser,cleanup,report}=require('./support.cjs');
(async()=>{
  const temp=testDirectory('recoding-inspection-');
  let server,browser,result;const errors=[];
  try {
    server=startServer(temp);const base=await server.ready;
    browser=await launchBrowser();const page=await browser.newPage({viewport:{width:1450,height:1000}});
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base,{waitUntil:'domcontentloaded'});
    await page.locator('#new-session').click();await page.locator('#session-submit').click();
    await page.waitForFunction(()=>state.session?.status==='sleeping'&&!state.switching);
    const compact=await page.evaluate(()=>{
      const box=id=>document.getElementById(id).getBoundingClientRect(),input=box('prompt'),send=box('send');
      return {input:input.height,send:send.height,body:box('composer-body').height,
        aligned:Math.abs(input.bottom-send.bottom)<2,tipsAbove:document.querySelector('.composer-hint').getBoundingClientRect().bottom<=input.top,
        separated:box('detail-visibility').left-document.querySelector('.composer-tools').getBoundingClientRect().right};
    });
    assert(compact.input<=24&&compact.send<=28&&compact.body<=40,JSON.stringify(compact));
    assert(compact.aligned&&compact.tipsAbove&&compact.separated>=12,JSON.stringify(compact));
    assert.equal(await page.locator('#toggle-composer').count(),0);
    await page.locator('#prompt').fill(Array(60).fill('多行内容').join('\n'));
    assert.equal(await page.locator('#prompt').evaluate(node=>getComputedStyle(node).overflowY),'auto');
    await page.locator('#stop').click();await page.waitForFunction(()=>state.session.status==='stopped');
    await page.locator('#detail-visibility button[data-detail-mode=all]').click();
    await page.evaluate(()=>{$('toast').hidden=true;saveComposerPreference({hidden:true});loadComposerPreferences();});
    assert.equal(await page.locator('#composer-body').isVisible(),true,'Obsolete collapsed preferences cannot hide the only input');
    await page.evaluate(base64=>{
      const data=new DataTransfer();data.items.add(new File([Uint8Array.from(atob(base64),c=>c.charCodeAt(0))],'figure.jpg',{type:'image/jpeg'}));
      $('prompt').dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));
    },fs.readFileSync(path.join(__dirname,'../fixtures/small.jpg')).toString('base64'));
    await page.waitForFunction(()=>currentDraft().images[0]?.status==='ready');
    await page.locator('#prompt').fill('QUIET_SEND');await page.locator('#send').click();
    await page.waitForFunction(()=>!currentDraft().submitting&&$('prompt').value==='');
    assert.equal(await page.locator('#toast').isVisible(),false,'Successful queueing has no popup');
    await page.evaluate(()=>{
      const event=[...state.rootHistory.values()].find(event=>event.type==='user_input_queued'&&event.content==='QUIET_SEND');
      const image={...event.attachments[0],path:'figure.jpg',source:'/workspace/figure.jpg'};
      const peer='b'.repeat(32),run_id='rich-details';state.session.agents=[{id:peer,name:'Research peer',status:'sleeping',created_at:Date.now()/1000}];
      const emit=event=>dispatch({run_id,timestamp:Date.now()/1000,...event,sequence:state.sequence+1});
      const content='## Evidence\n\n**Formatted message** with $x^2$ and a [source](https://example.com).\n\n```python\ndef solve(x):\n    return x + 1\n```\n\n![Generated chart](figure.jpg)';
      window.inspectionFixture={emit,image,peer,content};
      emit({type:'loop_start'});
      emit({type:'agent_messages',step:101,messages:[{id:'peer-in',source:peer,target:'master',content,images:[image],input_order:0}]});
      emit({type:'model_start',step:101,model:'grok-4.7'});
      emit({type:'model_end',step:101,model:'grok-4.7',content,images:[image],tool_calls:1,elapsed:1,
        reasoning_content:'Reviewing evidence. '.repeat(100),usage:{completion_tokens:30}});
      for(const [index,name] of ['send_agent_message','broadcast_agent_message'].entries()) {
        const call_id=`rich-send-${index}`,args={message:content,images:['figure.jpg'],...(index===0?{agent_id:peer}:{})};
        emit({type:'tool_start',step:101,call_id,number:index+1,name,arguments:JSON.stringify(args)});
        emit({type:'tool_end',step:101,call_id,number:index+1,name,elapsed:0.01,result:{images:[image],recipients:[{agent_id:peer,message_id:`out-${index}`} ]}});
      }
      emit({type:'human_message',source:'master',target:'user',message_id:'rich-human',content,images:[image]});
    });
    const received=page.locator('.peer-message[data-message-id="peer-in"]');
    const output=page.locator('.model-call[data-step="101"] .model-output');
    if(await output.locator('.model-output-toggle').isVisible())await output.locator('.model-output-toggle').click();
    for(const action of ['send_agent_message','broadcast_agent_message'])await page.locator(`.tool-call[data-action=${action}] > summary`).click();
    for(const body of [received.locator('.peer-message-body'),output.locator('.rich-message-body'),
      page.locator('.peer-message[data-direction=outgoing] .peer-message-body').nth(0),page.locator('.peer-message[data-direction=outgoing] .peer-message-body').nth(1)]) {
      assert.equal(await body.locator('h2').innerText(),'Evidence');
      assert.equal(await body.locator('.katex').count(),1);assert(await body.locator('code .hljs-keyword').count()>0);
      assert.equal(await body.locator('a[href="https://example.com/"]').count()+await body.locator('a[href="https://example.com"]').count(),1);
      assert.equal(await body.locator('img').count(),1,'Inline image is not repeated as a gallery');
      await body.locator('img').evaluate(image=>image.decode());
      assert.match(await body.locator('img').getAttribute('src'),/^\/api\/sessions\/[^/]+\/images\/[a-f0-9]{64}$/);
      assert.equal(await body.evaluate(node=>getComputedStyle(node.closest('.peer-message') || node).backgroundImage),'none');
    }
    assert.equal(await page.locator('.tool-call[data-action=send_agent_message] > .message-tool-info').getAttribute('open'),null);
    const longArgs={agent_id:'b'.repeat(32),message:'## Full message\n\n'+'Detailed evidence. '.repeat(1000)+'\n\n**END_OF_MESSAGE**'};
    await page.route('**/tool-source?*',route=>route.fulfill({json:{source:JSON.stringify(longArgs,null,2),language:'json'}}));
    await page.evaluate(args=>{
      const {emit}=inspectionFixture,source=JSON.stringify(args,null,2);
      emit({type:'tool_start',step:101,call_id:'long-message',number:3,name:'send_agent_message',arguments:JSON.stringify(args).slice(0,16000),
        source_preview:source.slice(0,16000),source_truncated:true,source_length:source.length,source_language:'json'});
      emit({type:'tool_end',step:101,call_id:'long-message',number:3,name:'send_agent_message',elapsed:0.01,result:{recipients:[{agent_id:args.agent_id,message_id:'long-sent'}]}});
    },longArgs);
    const longMessage=page.locator('.tool-call').filter({has:page.locator('.tool-source-note')});
    await longMessage.locator(':scope > summary').click();
    await page.locator('.peer-message[data-direction=outgoing]').last().locator('strong').filter({hasText:'END_OF_MESSAGE'}).waitFor();
    assert.equal(await longMessage.locator('.message-route .message-recipient').innerText(),'Research peer');
    const thinking=page.locator('.model-call[data-step="101"] .model-summary-content');
    assert(await thinking.evaluate(node=>node.clientHeight<=35&&parseFloat(getComputedStyle(node).fontSize)<=11));
    await page.locator('.model-call[data-step="101"] .model-summary-toggle').click();
    assert(await thinking.evaluate(node=>node.clientHeight>35),'Full thinking remains readable when expanded');
    await page.locator('#detail-visibility button[data-detail-mode=human]').click();
    assert.equal(await output.isVisible(),false);assert.equal(await received.isVisible(),false);
    assert.equal(await page.locator('.human-message[data-message-id=rich-human]').isVisible(),true);
    await page.locator('#detail-visibility button[data-detail-mode=all]').click();assert.equal(await output.locator('h2').isVisible(),true);
    await page.evaluate(()=>{
      const {emit}=inspectionFixture;
      for(let i=0;i<12;i++)emit({type:'human_message',source:'master',target:'user',message_id:`scroll-${i}`,images:[],content:`## Report ${i}\n\n${'A readable result. '.repeat(35)}`});
      $('conversation-scroll').scrollTop=0;
    });
    await page.locator('#jump-to-latest').waitFor();await page.locator('#jump-to-latest').click();
    await page.waitForFunction(()=>{const node=$('conversation-scroll');return node.scrollHeight-node.scrollTop-node.clientHeight<3;});
    await page.locator('#jump-to-latest').waitFor({state:'hidden'});
    for(const width of [700,390,320]) {
      await page.setViewportSize({width,height:900});
      await page.waitForFunction(()=>document.documentElement.scrollWidth<=innerWidth);
      assert.equal(await page.locator('#send').isVisible(),true);assert.equal(await page.locator('.composer-hint').isVisible(),true);
      const contained=await page.locator('.runtime-line').evaluate(node=>[...node.children].filter(child=>child.checkVisibility()).every(child=>child.getBoundingClientRect().right<=innerWidth));
      assert(contained,`Input toolbar remains contained at ${width}px`);
    }
    assert.deepEqual(errors,[]);
    result={passed:true,checks:['Quiet send, 24px input / 26px Send, separated toolbar, no collapse control',
      'SEND, broadcast, RECEIVE and model output share formatted Markdown, math, highlighted code and server images without duplicates',
      'Compact thinking expands; model/peer content remains internal detail; raw delivery data stays nested',
      'Latest button follows the selected Agent scroller; narrow layouts stay contained'],external_model_api_calls:0,full_suite_run:false,errors};
  } finally {await cleanup({browser,server,temp});}
  report(result);
})().catch(error=>{console.error(error);process.exitCode=1;});
