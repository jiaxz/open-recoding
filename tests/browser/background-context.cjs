// Non-modal maintenance, compact model selector, and one inline image per message.
const assert=require('node:assert/strict');
const {testDirectory,startServer,launchBrowser,cleanup,report}=require('./support.cjs');
(async()=>{
  const temp=testDirectory('recoding-background-context-ui-');let server,browser;const errors=[],cancelled=[];
  try{
    server=startServer(temp);const base=await server.ready;browser=await launchBrowser();
    const page=await browser.newPage({viewport:{width:1500,height:950}});
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base,{waitUntil:'domcontentloaded'});
    await page.locator('#new-session').click();await page.locator('#session-submit').click();
    await page.waitForFunction(()=>state.session?.status==='sleeping'&&!state.switching);
    assert.equal(await page.locator('#composer-tips-button').count(),0);
    assert.equal(await page.locator('.composer-hint').textContent(),'任意时刻输入，Agent立即响应。');
    const model=page.locator('.model-card');
    assert((await model.boundingBox()).height<90,'Model selector stays compact');
    assert.equal(await page.locator('#model-mode').evaluate(node=>getComputedStyle(node).fontSize),'9px');
    await page.locator('#prompt').fill('后台维护不要影响我的草稿');
    await page.route('**/context-compaction',async route=>{
      cancelled.push(route.request().postDataJSON());
      await route.fulfill({status:202,contentType:'application/json',body:JSON.stringify({cancelled:true})});
    });
    await page.evaluate(()=>{
      const operation={request_id:'background-ui-test',agent_id:'master',name:'Master',status:'running',model:'grok-4.7'};
      dispatch({type:'context_compaction_session',timestamp:Date.now()/1000,sequence:state.sequence+1,operation});
    });
    const panel=page.locator('#context-maintenance');assert.equal(await panel.isVisible(),true);
    assert.match(await panel.textContent(),/Master/);
    assert((await panel.boundingBox()).y+(await panel.boundingBox()).height<=(await model.boundingBox()).y,'Maintenance appears above Session Model');
    assert.equal(await page.locator('#prompt').isEnabled(),true);
    assert.equal(await page.locator('#prompt').inputValue(),'后台维护不要影响我的草稿');
    assert.equal(await page.locator('dialog[open]').count(),0,'Background compression never opens a modal');
    await panel.getByRole('button',{name:'取消 Master 的上下文压缩'}).click();
    assert.deepEqual(cancelled,[{request_id:'background-ui-test'}]);
    const imageCount=await page.evaluate(()=>{
      const body=document.createElement('div'),picture={sha256:'a'.repeat(64),source:'/workspace/inline.png',path:'/workspace/.recoding/inputs/inline.png',width:1,height:1};
      renderMessageBody(body,'正文 ![图](/workspace/inline.png)',[picture],'master');
      return {inline:body.querySelectorAll('img').length,gallery:body.querySelectorAll('.image-gallery').length};
    });
    assert.deepEqual(imageCount,{inline:1,gallery:0});
    await page.evaluate(()=>dispatch({type:'context_compaction_session',timestamp:Date.now()/1000,sequence:state.sequence+1,
      operation:{request_id:'background-ui-test',agent_id:'master',name:'Master',status:'cancelled'}}));
    assert.equal(await panel.isVisible(),false);assert.equal(await page.locator('#prompt').isEnabled(),true);
    for(const width of [700,390,320]){
      await page.setViewportSize({width,height:950});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`Input hint overflow at ${width}px`);
    }
    assert.deepEqual(errors,[]);
    report({passed:true,checks:['New input hint replaces Tips','Session Model card stays compact with small explanatory text',
      'Background compression appears at the top of the inspector, leaves draft/input untouched and can be cancelled',
      'Inline message image renders once without a duplicate attachment gallery'],errors,external_model_api_calls:0,full_suite_run:false});
  }finally{await cleanup({browser,server});}
})().catch(error=>{console.error(error);process.exitCode=1;});
