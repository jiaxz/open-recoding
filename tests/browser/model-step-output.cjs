// One focused local-model flow for per-call notes; no real API or existing Session.
const assert=require('node:assert/strict'),https=require('node:https');
const {testDirectory,startServer,launchBrowser,cleanup,report,localTLS,listen}=require('./support.cjs');

(async()=>{
  const root=testDirectory('recoding-step-output-'),requests=[],errors=[],checks=[];
  let server,browser,model;
  try {
    const {cert,options}=localTLS(root);
    const short='已核对输入，接下来等待新的任务消息。';
    const long='**检查方案**：核对 $6 \\times 7$，再验证工具结果。\n\n'
      + Array.from({length:12},(_,i)=>`第 ${i+1} 项：保留证据，完成后再报告结果。`).join('\n\n')
      + '\n\n```python\nprint(6 * 7)\n```\n\nEND_OF_OUTPUT <script>window.outputUnsafe=true</script>';
    model=https.createServer(options,(req,res)=>{
      let body='';req.on('data',data=>{body+=data;});req.on('end',()=>{
        const request=JSON.parse(body);requests.push(request);const index=requests.length;
        const delta={role:'assistant',content:index===1?short:index===2?long:index===3?' \n ':'已确定下一步检查方向。'};
        if(index===5)delta.content=null;
        if(index!==4)delta.tool_calls=[{index:0,id:`sleep-${index}`,type:'function',function:{name:'sleep',
          arguments:JSON.stringify({seconds:300,...(index===5?{}:{_step_note:'工具参数提供的本步说明。'})})}}];
        if(index===5)delta.tool_calls=[
          {index:0,id:'human-5',type:'function',function:{name:'send_user_message',arguments:JSON.stringify({message:'已检查配置，暂未发现错误。'})}},
          {index:1,id:'sleep-5',type:'function',function:{name:'sleep',arguments:'{"seconds":300}'}}
        ];
        if(index===2)delta.reasoning_content='Independent reasoning summary.';
        if(index===3)delta.thinking='**Thinking 分片**：先核对输入，再读取结果。';
        if(index===4)delta.reasoning='模型返回的 reasoning 文本。';
        if(index===5){delta.thinking={signature:'opaque-thinking-not-text'};delta.difrost_reasoning={value:'opaque-reasoning-not-text'};}
        res.writeHead(200,{'Content-Type':'text/event-stream'});
        res.end('data: '+JSON.stringify({choices:[{index:0,delta,finish_reason:delta.tool_calls?'tool_calls':'stop'}],
          usage:{prompt_tokens:200,completion_tokens:40,total_tokens:240}})+'\n\ndata: [DONE]\n\n');
      });
    });
    await listen(model);
    server=startServer(root,{demo:false,env:{RECODING_API_KEY:'local-step-output-fixture',
      RECODING_BASE_URL:`https://localhost:${model.address().port}/v1`,SSL_CERT_FILE:cert,NO_PROXY:'localhost,127.0.0.1',no_proxy:'localhost,127.0.0.1'}});
    const base=await server.ready;browser=await launchBrowser();
    const page=await browser.newPage({viewport:{width:1440,height:1000}});page.setDefaultTimeout(12000);
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base,{waitUntil:'domcontentloaded'});
    await page.waitForFunction(()=>state.config&&workspaceSync.loaded);
    await page.locator('#new-session').click();await page.locator('#session-name').fill('Step output fixture');
    await page.locator('#session-submit').click();
    await page.waitForFunction(()=>state.session&&!state.switching);
    await page.evaluate(()=>selectAgent('master'));
    await page.locator('#detail-visibility button[data-detail-mode=all]').click();
    const card=step=>page.locator(`.model-call[data-step="${step}"]`);
    await card(1).locator('.model-output').waitFor({state:'visible'});
    assert.match(requests[0].messages[0].content,/In EVERY model response, include a brief step note/);
    assert.match(requests[0].messages[0].content,/returning tool calls or sleep/);
    assert(requests[0].tools.every(tool=>tool.function.parameters.required.includes('_step_note')));
    assert.equal(await card(1).locator('.model-response').innerText(),short);
    assert.equal(await card(1).locator('.model-output-toggle').isVisible(),false,'A short note needs no expand control');
    assert.equal(await card(1).locator('.model-summary').isVisible(),false,'No empty reasoning placeholder');
    assert.equal(await page.locator('.steps > .model-output').count(),0,'Output stays inside its Model call card');
    assert.equal(await page.locator('.human-message').count(),0,'Step notes are not human messages');
    assert.equal(await card(1).evaluate(node=>{
      const children=[...node.children];
      return children.indexOf(node.querySelector('.model-output'))<children.indexOf(node.querySelector('.model-metrics'));
    }),true);
    checks.push('Real local SSE response with sleep shows its step note prominently inside the call; no duplicate or implicit human message');

    const send=async text=>{
      await page.locator('#prompt').fill(text);await page.locator('#prompt').press('Enter');
    };
    await send('继续检查');await card(2).locator('.model-output-toggle').waitFor({state:'visible'});
    const output=card(2).locator('.model-response'),toggle=card(2).locator('.model-output-toggle');
    const collapsed=await output.evaluate(node=>node.clientHeight);
    await toggle.click();assert.equal(await toggle.getAttribute('aria-expanded'),'true');
    assert(await output.evaluate(node=>node.clientHeight)>collapsed);
    assert.equal(await output.locator('strong').innerText(),'检查方案');
    assert.equal(await output.locator('.katex').count(),1);
    assert(await output.locator('code .hljs-built_in').count()>0);
    assert.match(await output.innerText(),/END_OF_OUTPUT/);
    assert.equal(await page.evaluate(()=>window.outputUnsafe),undefined);
    assert.equal(await card(2).locator('.model-summary-content').innerText(),'Independent reasoning summary.');
    await toggle.click();assert.equal(await toggle.getAttribute('aria-expanded'),'false');
    assert.equal(requests.length,2,'Displaying the note does not add model requests');
    checks.push('Long notes expand/collapse with Markdown, math and code highlighting; reasoning remains separate; unsafe HTML is inert');

    for(const mode of ['human','chat']) {
      await page.locator(`#detail-visibility button[data-detail-mode=${mode}]`).click();
      assert.equal(await card(2).isVisible(),false);
    }
    await page.locator('#detail-visibility button[data-detail-mode=all]').click();
    await page.evaluate(()=>{
      const snapshot={...state.session,events:[...state.rootHistory.values()],sequence:state.sequence};
      install(snapshot,{tail:false});
    });
    await card(2).locator('.model-output').waitFor({state:'visible'});
    assert.match(await output.innerText(),/END_OF_OUTPUT/);
    assert.equal(await page.locator('.steps > .model-output').count(),0);
    for(const width of [700,390,320]){
      await page.setViewportSize({width,height:950});
      await page.waitForFunction(()=>document.documentElement.scrollWidth<=innerWidth);
      assert.equal(await card(1).locator('.model-output').isVisible(),true);
    }
    await page.setViewportSize({width:1440,height:1000});
    await page.evaluate(()=>selectAgent('all'));
    assert.equal(await page.locator('.model-output,.model-call').count(),0,'All agents remains a message-only projection');
    await page.evaluate(()=>selectAgent('master'));
    await card(2).locator('.model-output').waitFor({state:'visible'});
    assert.equal(await page.locator('#toast').isVisible(),false,'Caught UI errors must not be hidden behind a toast');
    checks.push('History replay, Agent/All agents switching and detail modes retain the new placement; narrow layouts remain contained');
    if(process.env.AGENT_UI_SCREENSHOT){
      await card(1).scrollIntoViewIfNeeded();
      await page.screenshot({path:process.env.AGENT_UI_SCREENSHOT});
    }

    await send('不产生文本');await card(3).locator('.model-status').filter({hasText:'done'}).waitFor();
    assert.equal(await card(3).locator('.model-response').innerText(),'工具参数提供的本步说明。');
    assert.equal(await card(3).locator('.model-output').getAttribute('data-source'),'tool_arguments');
    assert.equal(await card(3).locator('.model-summary').isVisible(),true);
    assert.equal(await card(3).locator('.model-summary-content strong').innerText(),'Thinking 分片');
    assert.equal(await card(3).locator('.model-summary').getAttribute('aria-label'),'API 返回的 Thinking 文本');
    await send('仅输出文本');await card(4).locator('.model-output').waitFor({state:'visible'});
    assert.equal(await card(4).locator('.model-response').innerText(),'已确定下一步检查方向。');
    assert.equal(await card(4).locator('.model-summary-content').innerText(),'模型返回的 reasoning 文本。');
    assert.equal(await page.evaluate(()=>[...state.rootHistory.values()].some(event=>
      event.type==='tool_end'&&event.step===3&&!event.result?.error)),true);
    await send('不返回说明');await card(5).locator('.model-status').filter({hasText:'done'}).waitFor();
    assert.equal(await card(5).locator('.model-response').innerText(),'已检查配置，暂未发现错误。');
    assert.equal(await card(5).locator('.model-output').getAttribute('data-source'),'tool_message');
    await page.locator('.human-message').waitFor({state:'visible'});
    assert.equal(await card(5).locator('.model-summary').isVisible(),false,'Opaque thinking metadata is not readable text');
    assert.doesNotMatch(await page.locator('#messages').innerText(),/opaque-thinking-not-text|opaque-reasoning-not-text/);
    checks.push('Readable reasoning_content/thinking/reasoning text appears independently of the step note; missing or opaque thinking stays hidden');
    await page.locator('#stop').click();
    await page.waitForFunction(()=>state.session?.status==='stopped'&&state.session.read_only);
    assert.equal(requests.length,5);
    assert.equal(await page.locator('.human-message').count(),1);
    assert.deepEqual(errors,[]);
    checks.push('Tool-only responses use model-authored structured notes; execution strips metadata; outgoing message text is an attributed fallback; text-only responses work; driver explicitly stops the endless loop');
    report({passed:true,checks,model_calls:requests.length,external_model_api_calls:0,full_suite_run:false,errors});
  }finally{await cleanup({browser,server,model});}
})().catch(error=>{console.error(error);process.exitCode=1;});
