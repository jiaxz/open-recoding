// Focused terminal listener/page/stream lifecycle check; no model API calls.
const assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {testDirectory, startServer,launchBrowser,cleanup,report:writeReport}=require('./support.cjs');

(async()=>{
  const temp=testDirectory('recoding-server-status-');
  let server,browser,output='',report;const errors=[];let turnRequests=0;
  const wait=async predicate=>{
    const deadline=Date.now()+12000;
    while(!predicate()){
      if(Date.now()>deadline)throw new Error('Timed out waiting for terminal lifecycle status: '+output);
      await new Promise(resolve=>setTimeout(resolve,40));
    }
  };
  const count=event=>output.split('\n').filter(line=>line.includes(event)).length;
  try {
    server=startServer(temp);server.process.stdout.on('data',data=>{output+=data;});
    const base=await server.ready;
    assert.match(output,/\[\d{2}:\d{2}:\d{2}\] LISTENING/);
    assert(output.includes(base));assert.match(output,/instance=/);assert.match(output,/workspace=/);
    const logDirectory=path.join(temp,'.recoding','server-logs');
    const logPath=path.join(logDirectory,fs.readdirSync(logDirectory).find(name=>name.endsWith('.jsonl')));
    assert.equal(fs.statSync(logPath).mode&0o077,0);
    browser=await launchBrowser();const page=await browser.newPage();page.on('pageerror',error=>errors.push(error.message));
    page.on('request',request=>{if(new URL(request.url()).pathname.endsWith('/turn')||new URL(request.url()).pathname.endsWith('/input'))turnRequests++;});
    await page.goto(base,{waitUntil:'domcontentloaded'});await wait(()=>count('PAGE LOAD')===1);
    await page.locator('#new-session').click();await page.locator('#session-name').fill('private-fixture-session-name');
    await page.locator('#session-submit').click();
    await page.waitForFunction(()=>state.session?.status==='sleeping'&&state.source?.readyState===EventSource.OPEN);
    await wait(()=>count('WEB CONNECT')===1);
    const identity=await page.evaluate(()=>({page:state.pageId.slice(0,8),session:state.session.id.slice(0,12)}));
    assert(output.includes(`page="${identity.page}"`));assert(output.includes(`session="${identity.session}"`));
    assert.match(output,/WEB CONNECT.*streams=1/);
    const probe=await page.request.get(base+'/api/config?key=private-fixture-query-secret');
    const requestId=probe.headers()['x-recoding-request'];assert(requestId);assert.match(probe.headers()['server-timing'],/app;dur=/);

    await page.locator('#prompt').fill('private-fixture-draft-content');
    assert.equal(await page.locator('#composer-tips-button').count(),0);
    assert.equal(await page.locator('.composer-hint').textContent(),'任意时刻输入，Agent立即响应。');
    assert.equal(await page.locator('#prompt').inputValue(),'private-fixture-draft-content');
    assert.equal(turnRequests,0,'The input hint never submits a message');
    await page.request.post(base+'/api/diagnostics',{headers:{'X-Agent-Token':await page.evaluate(()=>state.config.csrf)},data:{
      kind:'open',page:await page.evaluate(()=>state.pageId),elapsed_ms:1200,samples:[{
        path:'/api/config?key=private-fixture-query-secret',duration_ms:1200,queue_connect_ms:1100,server_ms:2,
      }],operations:[],draft:'private-fixture-draft-content',key:'private-fixture-query-secret',
    }});

    await page.evaluate(()=>{state.source.close();state.source=null;});
    await wait(()=>count('WEB DISCONNECT')===1);
    assert.match(output,/WEB DISCONNECT.*browser disconnected.*streams=0/);
    await page.evaluate(()=>connectSessionEvents());
    await page.waitForFunction(()=>state.source?.readyState===EventSource.OPEN);
    await wait(()=>count('WEB RECONNECT')===1);
    assert.match(output,/WEB RECONNECT.*streams=1/);
    await page.close();await wait(()=>count('WEB DISCONNECT')===2);

    const closed=new Promise(resolve=>server.process.once('close',resolve));server.process.kill('SIGINT');await closed;
    assert.match(output,/LISTENER STOPPED.*streams=0/);
    assert(!output.includes('private-fixture-query-secret'));
    assert(!output.includes('private-fixture-session-name'));
    const raw=fs.readFileSync(logPath,'utf8'),records=raw.trim().split('\n').map(line=>JSON.parse(line));
    assert(records.some(record=>record.event==='LISTENING'));
    assert(records.some(record=>record.event==='LISTENER STOPPED'));
    assert(records.some(record=>record.event==='TCP_ACCEPT'));
    assert(records.some(record=>record.event==='SESSION_OPERATION'&&record.stages.some(stage=>stage.stage==='history')));
    const headers=records.find(record=>record.event==='HTTP_HEADERS_READ'&&record.request===requestId);
    assert.equal(headers.route,'/api/config');
    const ended=records.find(record=>record.event==='HTTP_REQUEST_END'&&record.request===requestId);
    assert.equal(ended.status,200);assert(ended.elapsed_ms>=0);assert(ended.send_ms>=0);
    assert(records.some(record=>record.event==='BROWSER_TIMING'&&record.samples.some(sample=>sample.queue_connect_ms===1100&&sample.server_ms===2&&sample.route==='/api/config')));
    assert(records.some(record=>record.event==='BROWSER_TIMING'&&record.kind==='startup'));
    for(const secret of ['private-fixture-query-secret','private-fixture-session-name','private-fixture-draft-content'])assert(!raw.includes(secret));
    assert.deepEqual(errors,[]);
    report={passed:true,checks:[
      'Terminal shows a timestamped listening URL, PID, instance and workspace',
      'Page load and actual SSE connect/disconnect/reconnect carry stable page/session IDs and accurate stream counts',
      'Closing the browser releases the stream and stopping the server reports LISTENER STOPPED',
      'Request query secrets and Session names are absent from the status log',
      'Private per-start JSONL logs persist request IDs, TCP/HTTP/Session phases and numeric browser timings; query strings, drafts and injected extra fields are excluded',
      'The input hint preserves the draft and never auto-sends',
    ],errors,external_model_api_calls:0,full_suite_run:false};
  } finally {await cleanup({browser,server,temp});}
  writeReport(report);
})().catch(error=>{console.error(error);process.exitCode=1;});
