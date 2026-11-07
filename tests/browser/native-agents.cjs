// One focused V4 browser flow: isolated local TLS fixture, no project .env/API.
const assert=require('node:assert/strict'),fs=require('node:fs'),https=require('node:https'),os=require('node:os'),path=require('node:path'),crypto=require('node:crypto');
const {testDirectory, startServer,launchBrowser,localTLS,listen,cleanup,report}=require('./support.cjs');
(async()=>{
  const temp=testDirectory('recoding-continuous-ui-'),workspace=path.join(temp,'workspace');fs.mkdirSync(workspace);
  const jpeg=fs.readFileSync(path.join(__dirname,'../fixtures/small.jpg')),digest=crypto.createHash('sha256').update(jpeg).digest('hex');
  let browser,server,model,result,childId,created=false;const requests=[],errors=[],posts=[],handled=new Map(),checks=[];
  const textOf=message=>typeof message.content==='string'?message.content:(message.content||[]).filter(part=>part.type==='text').map(part=>part.text).join('\n');
  try{
    const {cert,options}=localTLS(temp);
    model=https.createServer(options,(req,res)=>{let raw='';req.on('data',chunk=>raw+=chunk);req.on('end',()=>{
      try{
        const body=JSON.parse(raw),isMaster=body.messages[0].content.includes('Your role is MASTER;'),id=isMaster?'master':body.messages[0].content.match(/your agent_id is ([a-f0-9]{32})/)?.[1];assert(id);
        const seen=handled.get(id)||new Set();handled.set(id,seen);requests.push({id,body});
        const all=body.messages.map(textOf).join('\n'),tool=(name,args,index=0)=>({index,id:`fixture-${requests.length}-${index}`,type:'function',function:{name,arguments:JSON.stringify(args)}});
        let delta;
        if(isMaster&&!created){created=true;delta={content:'Internal master planning',tool_calls:[tool('create_agent',{name:'Evidence Inspector'})]};}
        else if(!isMaster&&!seen.has('file')){seen.add('file');delta={tool_calls:[tool('python',{code:`import base64\nfrom pathlib import Path\nPath('report.jpg').write_bytes(base64.b64decode('${jpeg.toString('base64')}'))`})]};}
        else if(!isMaster&&!seen.has('report')){seen.add('report');delta={tool_calls:[tool('send_user_message',{message:'## Inspector report\n\nVerified **evidence**. $x^2$\n\n![Evidence](report.jpg)',images:['report.jpg']})]};}
        else if(isMaster&&all.includes('DIRECT_IMAGE_V4')&&!seen.has('direct')){seen.add('direct');delta={tool_calls:[tool('send_agent_message',{agent_ids:[childId],message:'PEER_EVIDENCE_V4',images:['report.jpg']})]};}
        else if(!isMaster&&all.includes('PEER_EVIDENCE_V4')&&!seen.has('peer')){seen.add('peer');delta={tool_calls:[tool('send_user_message',{message:'Peer image received as a path. No automatic view_image.',images:[]})]};}
        else if(isMaster&&!seen.has('report')){seen.add('report');delta={tool_calls:[tool('send_user_message',{message:'## Master update\n\nWorker is independent. Your next message goes only to the selected Agent.',images:[]})]};}
        else if(isMaster){delta={content:'Internal notes only. No Human delivery.\n\n```python\nx = 1\n```\n\n![Internal diagram](report.jpg)'};}
        else delta={tool_calls:[tool('sleep',{seconds:300})]};
        res.writeHead(200,{'Content-Type':'text/event-stream'});res.end(`data: ${JSON.stringify({choices:[{index:0,delta,finish_reason:delta.tool_calls?'tool_calls':'stop'}],usage:{prompt_tokens:100,completion_tokens:20,total_tokens:120}})}\n\ndata: [DONE]\n\n`);
      }catch(error){errors.push('fixture: '+error.message);res.writeHead(500);res.end('Local fixture failure');}
    });});await listen(model);
    server=startServer(workspace,{demo:false,env:{GEMINI_API_KEY:'local-v4-ui-fixture',GEMINI_BASE_URL:`https://localhost:${model.address().port}/v1`,SSL_CERT_FILE:cert,NO_PROXY:'localhost,127.0.0.1'}});
    const base=await server.ready;browser=await launchBrowser();const page=await browser.newPage({viewport:{width:1480,height:1050}});
    page.on('pageerror',error=>errors.push(error.message));page.on('request',req=>{if(req.method()==='POST')posts.push(new URL(req.url()).pathname);});
    await page.goto(base,{waitUntil:'domcontentloaded'});await page.locator('#new-session').click();await page.locator('#session-name').fill('Continuous loop fixture');await page.locator('#session-submit').click();
    await page.waitForFunction(()=>state.session?.status==='sleeping'&&state.session.agents?.length===1&&state.session.agents[0].status==='sleeping'&&!state.switching,null,{timeout:20000});
    childId=await page.evaluate(()=>state.session.agents[0].id);const rootId=await page.evaluate(()=>state.session.id);
    assert.equal(await page.evaluate(()=>state.config.api_version),4);
    assert.equal(await page.locator('.turn,.turn-finish,.is-final-response,#input-target,#reset').count(),0);
    assert(await page.locator('.agent-run').count()>0);assert(await page.locator('.agent-sleep[data-origin=system]').count()>0);assert.equal(await page.locator('.model-call[data-status=running]').count(),0);
    assert.match(await page.locator('#runtime-label').innerText(),/休眠/);
    assert.equal(await page.locator('.human-message').count(),1);assert.equal(await page.locator('.human-message').filter({hasText:'Inspector report'}).count(),0);assert.equal(await page.locator('.human-message').filter({hasText:'Internal notes only'}).count(),0);
    await page.locator(`#agent-tabs [data-agent-id="${childId}"]`).click();await page.waitForFunction(id=>state.agentId===id,childId);
    const evidence=page.locator('.human-message').filter({hasText:'Inspector report'});assert.equal(await evidence.count(),1);assert.equal(await evidence.locator('img').count(),1);assert.equal(await evidence.locator('.katex').count(),1);
    const imageURL=await evidence.locator('img').getAttribute('src');assert(imageURL.includes(`/agents/${childId}/images/${digest}`));
    const image=await page.request.get(base+imageURL);assert.equal(image.status(),200);assert((await image.body()).equals(jpeg));
    checks.push('Each Agent page shows only its own explicit user messages, with sender-scoped remote image bytes and formulas');
    await page.locator('#agent-tabs > [data-agent-id=master]').click();await page.waitForFunction(()=>state.agentId==='master');

    await page.locator('#detail-visibility button[data-detail-mode=human]').click();
    assert.equal(await page.locator('.model-call:visible,.model-output:visible,.tool-call:visible,.peer-message:visible,.system-message:visible,.run-heading:visible').count(),0);
    assert.equal(await page.locator('.human-message:visible').count(),1);
    await page.locator('#toggle-agent-settings').click();
    await page.locator('#idle-sleep').fill('30');await page.locator('#idle-sleep').press('Tab');
    await page.waitForFunction(()=>state.session.team?.idle_sleep_seconds===30);
    await page.evaluate(base64=>{const data=new DataTransfer();data.items.add(new File([Uint8Array.from(atob(base64),c=>c.charCodeAt(0))],'clipboard.jpg',{type:'image/jpeg'}));document.querySelector('#prompt').dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));},jpeg.toString('base64'));
    await page.waitForFunction(()=>currentDraft().images[0]?.status==='ready');await page.locator('#prompt').fill('DIRECT_IMAGE_V4');await page.locator('#prompt').press('Enter');
    await page.waitForFunction(()=>[...state.humanMessages.values()].some(message=>message.content.includes('Peer image received')),null,{timeout:15000});
    assert.equal(await page.locator('.user-followup:visible').filter({hasText:'DIRECT_IMAGE_V4'}).count(),1);assert.equal(await page.locator('.human-message:visible').count(),1);assert.equal(await page.locator('.human-message').filter({hasText:'Peer image received'}).count(),0);assert.equal(await page.evaluate(id=>unreadCount(state.session.id,id),childId),1);
    assert(requests.some(request=>request.id==='master'&&request.body.messages.some(message=>textOf(message).includes('DIRECT_IMAGE_V4')&&Array.isArray(message.content)&&message.content.some(part=>part.type==='image_url'&&part.image_url.url.endsWith(jpeg.toString('base64'))))));
    const childPeerRequest=requests.find(request=>request.id===childId&&request.body.messages.some(message=>textOf(message).includes('PEER_EVIDENCE_V4')));assert(childPeerRequest);assert(!childPeerRequest.body.messages.some(message=>Array.isArray(message.content)&&message.content.some(part=>part.type==='image_url')),'Peer images remain paths/metadata until explicit view_image');
    checks.push('Details filter retains only Human↔Agent messages; direct clipboard pixels reach only the selected model; idle configuration saves; a grouped peer image wakes sleep without adding pixels to recipient context');

    await page.locator('#detail-visibility button[data-detail-mode=all]').click();
    const plain=page.locator('.model-call .model-output').filter({hasText:'Internal notes only'}).first();if(await plain.locator('.model-output-toggle').isVisible())await plain.locator('.model-output-toggle').click();assert.equal(await plain.locator('.hljs-keyword,.hljs-number').count()>0,true);
    await page.locator(`#agent-tabs [data-agent-id="${childId}"]`).click();await page.waitForFunction(id=>state.agentId===id,childId);
    assert.equal(await page.locator('.human-message').count(),2);assert.equal(await page.locator('.human-message').filter({hasText:'Master update'}).count(),0);assert.equal(await page.locator('.agent-sleep[data-origin=model]').count()>0,true);
    assert.equal(await page.locator('.peer-message[data-direction=incoming]').filter({hasText:'PEER_EVIDENCE_V4'}).count(),1);
    await page.locator('#stop').click();await page.waitForFunction(()=>state.session.status==='stopped'&&state.session.agents.every(agent=>agent.status==='stopped'),null,{timeout:15000});
    await page.locator('#prompt').fill('Stopped draft');assert.equal(await page.locator('#send').isDisabled(),false);assert.equal(await page.locator('#continue-task').isVisible(),true);
    await page.locator('#continue-task').click();await page.waitForFunction(()=>state.session.status==='sleeping',null,{timeout:15000});assert.equal(await page.evaluate(()=>state.session.agents[0].status),'stopped');
    await page.locator('#agent-tabs > [data-agent-id=master]').click();await page.waitForFunction(()=>state.agentId==='master');assert.equal(await page.locator('.human-message').count(),1);assert.equal(await page.locator('.human-message').filter({hasText:'Inspector report'}).count(),0);
    assert.equal(posts.some(url=>/\/(broadcast|turn|reset)$/.test(url)),false);assert.equal(await page.evaluate(()=>state.source!==null),true);
    checks.push('Model prose stays expandable details with syntax highlighting; Agent navigation preserves independent human-message histories; Stop All keeps human queuing available and Resume Master starts only Master while preserving messages');

    const rates=await page.evaluate(()=>{
      const samples=[{timestamp:100,elapsed:10,output_tokens:100},{timestamp:100,elapsed:10,output_tokens:100}];
      const sequential=[samples[0],{timestamp:110,elapsed:10,output_tokens:100}];
      const sum=AgentStatistics.sessionOutputRate(samples,100),serial=AgentStatistics.sessionOutputRate(sequential,110);
      const clock=Date.now;Date.now=()=>100000;
      try {
        AgentStatistics.renderSpeed(document.querySelector('#session-speed'),samples.map((sample,i)=>({...sample,rate:10,run_id:'sum',step:i+1,model:'gemini-4-argon'})),'numeric-fixture');
        return {sum,serial,curve:Number(document.querySelector('.speed-chart').dataset.latestRate)};
      }finally{Date.now=clock;}
    });assert.deepEqual(rates,{sum:20,serial:10,curve:20});
    for(const width of [390,320]){await page.setViewportSize({width,height:900});await page.waitForFunction(()=>document.documentElement.scrollWidth<=innerWidth);}
    assert.deepEqual(errors,[]);checks.push('Session smoothing adds simultaneous Agent throughput (20 tok/s) rather than averaging; sequential work stays 10 tok/s, and narrow layouts remain contained');
    result={passed:true,checks,local_model_requests:requests.length,external_model_api_calls:0,errors,full_suite_run:false};
  }finally{await cleanup({browser,server,model,temp});}
  report(result);
})().catch(error=>{console.error(error);process.exitCode=1;});
