// Focused real worker + local HTTPS fixture: retries, Master labels and SSE replacement.
const assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),https=require('node:https');
const {testDirectory, startServer,launchBrowser,localTLS,listen,cleanup,report}=require('./support.cjs');

(async()=>{
  const temp=testDirectory('recoding-master-streams-');
  let server,browser,model,output='',result;const requests=[],errors=[];
  const streamCounts=()=>output.split('\n').filter(line=>/WEB (CONNECT|SWITCH|RECONNECT)/.test(line)).map(line=>Number(line.match(/streams=(\d+)/)[1]));
  try {
    model=https.createServer(localTLS(temp).options,(req,res)=>{
      let raw='';req.on('data',chunk=>raw+=chunk);req.on('end',()=>{
        requests.push(JSON.parse(raw));const number=requests.length;
        if(number===1){res.writeHead(503,{'Content-Type':'application/json'});res.end('{"error":"temporary fixture failure"}');return;}
        res.writeHead(200,{'Content-Type':'text/event-stream'});
        const delta=number===2 ? {tool_calls:[{index:0,id:'write-once',type:'function',function:{name:'python',arguments:JSON.stringify({code:"from pathlib import Path\np=Path('counter.txt')\np.write_text(str(int(p.read_text())+1) if p.exists() else '1')\nprint(p.read_text())"})}}]} :
          number===3 ? {tool_calls:[{index:0,id:'partial-must-not-run',type:'function',function:{name:'python',arguments:'{"code":"raise '}}]} : {content:'## 完成\n\n工具只执行一次。'};
        res.write(`data: ${JSON.stringify({choices:[{index:0,delta}]})}\n\n`);
        if(number===3){res.end();return;}
        res.end(`data: ${JSON.stringify({choices:[{index:0,delta:{},finish_reason:'stop'}],usage:{prompt_tokens:20,completion_tokens:10,total_tokens:30}})}\n\ndata: [DONE]\n\n`);
      });
    });
    await listen(model);
    server=startServer(temp,{demo:false,env:{GEMINI_API_KEY:'local-fixture-only',GEMINI_BASE_URL:`https://localhost:${model.address().port}/v1`,SSL_CERT_FILE:path.join(temp,'cert.pem'),NO_PROXY:'localhost,127.0.0.1'}});
    server.process.stdout.on('data',data=>output+=data);
    const base=await server.ready;
    // Old metadata must be invisible even when an old registry points at it.
    const oldFolder=path.join(temp,'.recoding','old-fixture');fs.mkdirSync(oldFolder,{recursive:true});
    const old=JSON.stringify({format:'recoding.session',version:1,id:'a'.repeat(32),name:'Hidden old fixture',created_at:new Date().toISOString()});
    fs.writeFileSync(path.join(oldFolder,'session.json'),old);
    browser=await launchBrowser();const page=await browser.newPage();page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base,{waitUntil:'domcontentloaded'});
    await page.locator('#new-session').click();await page.locator('#session-name').fill('First');await page.locator('#session-submit').click();
    const ready=()=>page.waitForFunction(()=>state.session?.status==='ready'&&state.source?.readyState===EventSource.OPEN);
    await ready();
    assert.equal(await page.locator('#agent-tabs [data-agent-id=master] strong').textContent(),'Master');
    assert.equal(await page.locator('.session-item').filter({hasText:'Hidden old fixture'}).count(),0);
    await page.locator('#prompt').fill('Write once, then report');await page.locator('#prompt').press('Enter');
    await page.locator('.model-status').first().filter({hasText:'retry 2/4'}).waitFor();
    await page.waitForFunction(()=>[...state.eventLog.values()].some(e=>e.type==='answer'));
    await ready();assert.equal(requests.length,4);
    assert.deepEqual(requests[0],requests[1]);assert.deepEqual(requests[2],requests[3]);
    assert.match(requests[0].messages[0].content,/Your role is MASTER; your agent_id is master/);
    assert.equal(fs.readFileSync(path.join(temp,'counter.txt'),'utf8'),'1');
    assert.equal(await page.locator('.tool-call').count(),1,'Partial tool responses must never execute');
    assert.equal(await page.locator('.model-status').filter({hasText:'done · 1 retry'}).count(),2);
    const first=await page.evaluate(()=>state.session.id);
    await page.evaluate(async()=>{await openSession({action:'create',name:'Second'});});await ready();
    const second=await page.evaluate(()=>state.session.id);
    for(const id of [first,second,first,second,first]){
      await page.evaluate(id=>openSession({action:'resume',id}),id);await ready();
    }
    // Keep old upstream connections open deliberately, as a proxy may do even
    // after the browser closes EventSource. The server must retire each one.
    await page.evaluate(async()=>{
      window.probes=[];
      for(let n=0;n<4;n++){
        const source=new EventSource(`/api/sessions/${state.session.id}/events?after=${state.sequence}&instance=${state.config.instance_id}&page=${state.pageId}`);
        window.probes.push(source);
        await new Promise((resolve,reject)=>{source.onopen=()=>{source.close();resolve();};source.onerror=()=>reject(new Error('probe failed'));});
      }
    });
    // A separate raw client has no browser-close notification; duplicate page
    // IDs still replace the previous server-side connection immediately.
    const http=require('node:http'),held=[];
    const identity=await page.evaluate(()=>({page:state.pageId,id:state.session.id,instance:state.config.instance_id}));
    for(let n=0;n<4;n++)await new Promise((resolve,reject)=>{
      const request=http.get(`${base}/api/sessions/${identity.id}/events?page=${identity.page}&instance=${identity.instance}`,response=>{
        held.push({request,response});response.on('error',()=>{});response.on('data',()=>{});resolve();
      });request.on('error',reject);
    });
    const deadline=Date.now()+3000;
    while(held.slice(0,-1).some(item=>!item.response.destroyed)&&Date.now()<deadline)await new Promise(resolve=>setTimeout(resolve,20));
    assert(held.slice(0,-1).every(item=>item.response.destroyed),'Replaced upstream sockets must actually close, not only decrement a counter');
    for(const item of held)item.request.destroy();
    const counts=streamCounts();assert(counts.length>=10);assert(counts.every(count=>count===1),JSON.stringify(counts));
    const oldOpen=await page.request.post(`${base}/api/sessions`,{headers:{'X-Agent-Token':await page.evaluate(()=>state.config.csrf)},data:{action:'resume',path:oldFolder}});
    assert.equal(oldOpen.status(),400);assert.equal(fs.readFileSync(path.join(oldFolder,'session.json'),'utf8'),old);
    assert.deepEqual(errors,[]);
    result={passed:true,checks:['Master names/IDs and current format v2','503 and incomplete SSE retry in the real worker; identical request/context; tool executes once','Retry status is visible; prior failures retained in diagnostics','Repeated Session switches and deliberately retained upstream streams stay at one subscription per page','Old Session hidden, explicit Resume rejected, old bytes unchanged'],subscriptions_checked:counts.length,external_model_api_calls:0,full_suite_run:false};
  } finally {await cleanup({browser,server,model,temp});}
  report(result);
})().catch(error=>{console.error(error);process.exitCode=1;});
