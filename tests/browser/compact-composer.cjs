// Focused compact-input and conversation-position regression; offline only.
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {testDirectory, startServer,launchBrowser,cleanup,report}=require('./support.cjs');
(async()=>{
  const temp=testDirectory('recoding-composer-');
  let server,browser,result;const errors=[];
  try {
    server=startServer(temp);const base=await server.ready;
    browser=await launchBrowser();const page=await browser.newPage({viewport:{width:1450,height:1050}});
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base,{waitUntil:'domcontentloaded'});
    await page.locator('#new-session').click();await page.locator('#session-submit').click();
    await page.waitForFunction(()=>state.session?.status==='sleeping'&&!state.switching);
    const dimensions=()=>page.evaluate(()=>{
      const input=document.querySelector('#prompt'),send=document.querySelector('#send'),tips=document.querySelector('.composer-hint');
      const box=input.getBoundingClientRect();return {height:box.height,body:document.querySelector('#composer-body').getBoundingClientRect().height,
        sendBottom:send.getBoundingClientRect().bottom-box.bottom,tipsAbove:tips.getBoundingClientRect().bottom<=box.top,
        scroll:input.scrollHeight>input.clientHeight+1,overflow:getComputedStyle(input).overflowY};
    });
    assert.equal(await page.locator('#prompt').getAttribute('rows'),'1');
    const empty=await dimensions();assert(empty.height<=32);assert(empty.body<75);assert(Math.abs(empty.sendBottom)<2);assert(empty.tipsAbove);
    await page.locator('#prompt').fill('第一行\n第二行\n第三行');
    const growing=await dimensions();assert(growing.height>empty.height+20);assert.equal(growing.scroll,false);
    await page.locator('#prompt').fill(Array(80).fill('中文长文本和换行').join('\n'));
    const capped=await dimensions();assert(capped.height<=160);assert(capped.scroll);assert.equal(capped.overflow,'auto');
    await page.locator('#prompt').fill('只发送一行');await page.locator('#prompt').press('Enter');
    await page.waitForFunction(()=>document.querySelector('#prompt').value==='');
    assert.equal((await dimensions()).height,empty.height,'Sending returns to one compact line');
    await page.waitForFunction(()=>state.session.status==='sleeping');

    await page.evaluate(()=>{
      const run_id='position-fixture',emit=event=>dispatch({timestamp:Date.now()/1000,run_id,...event,sequence:state.sequence+1});
      emit({type:'loop_start'});
      for(let i=0;i<16;i++) {
        const step=201+i,id=`u${i}`,content=`用户问题 ${i}`;
        emit({type:'user_input_queued',message_id:id,target:'master',content,attachments:[]});
        emit({type:'user_input_applied',message_id:id,target:'master',content,attachments:[],step,input_order:0});
        emit({type:'model_start',step,model:'grok-4.7'});
        emit({type:'model_end',step,model:'grok-4.7',elapsed:1,content:`执行说明 ${i}`,tool_calls:1,usage:{completion_tokens:30}});
        emit({type:'tool_start',number:i+1,name:'python',arguments:'{"code":"print(1)"}'});
        emit({type:'tool_end',number:i+1,name:'python',elapsed:0.01,result:{stdout:'1'}});
        emit({type:'human_message',source:'master',target:'user',message_id:`a${i}`,images:[],
          content:`## 回复 ${i}\n\n这是一段可读的报告，保留正在阅读的位置。\n\n- 第一项证据\n- 第二项发现\n- 后续建议`});
      }
    });
    const reply=page.locator('.human-message[data-message-id=a7]');
    const top=locator=>locator.evaluate(node=>node.getBoundingClientRect().top-document.querySelector('#conversation-scroll').getBoundingClientRect().top);
    await reply.evaluate(node=>{
      const scroller=document.querySelector('#conversation-scroll');scroller.scrollTop+=node.getBoundingClientRect().top-scroller.getBoundingClientRect().top-100;
    });
    const reading=await top(reply);
    await page.locator('#detail-visibility button[data-detail-mode=human]').click();
    assert(Math.abs(await top(reply)-reading)<3,'Hiding all details keeps the visible conversation anchor');
    await page.locator('#detail-visibility button[data-detail-mode=all]').click();
    const shown=await top(reply);assert(Math.abs(shown-reading)<3,`Showing all details keeps the visible conversation anchor: ${reading} → ${shown}`);
    await page.locator('#detail-visibility button[data-detail-mode=human]').click();
    await page.evaluate(()=>scrollToEnd());await page.locator('#detail-visibility button[data-detail-mode=all]').click();
    const bottom=()=>page.evaluate(()=>{const node=document.querySelector('#conversation-scroll');return node.scrollHeight-node.scrollTop-node.clientHeight;});
    assert(await bottom()<3,'At the end, global detail expansion continues following the end');
    await page.locator('#detail-visibility button[data-detail-mode=human]').click();assert(await bottom()<3);
    for(const width of [700,390,320]) {
      await page.setViewportSize({width,height:900});
      await page.waitForFunction(()=>document.documentElement.scrollWidth<=innerWidth);
      await page.locator('#prompt').fill('单行文本');assert((await dimensions()).height<=32);
    }
    assert.deepEqual(errors,[]);
    result={passed:true,checks:['One-line input grows with content, caps its height and scrolls; Send shares its row and Tips sit above',
      'Sending shrinks the input; compact layout remains contained on narrow screens',
      'Global detail toggles preserve a middle reading anchor and continue following the end only when already there',
      'Three-mode display keeps conversation positions stable while Human-boundary detail buttons remain available'],
      external_model_api_calls:0,errors,full_suite_run:false};
  } finally {await cleanup({browser,server,temp});}
  report(result);
})().catch(error=>{console.error(error);process.exitCode=1;});
