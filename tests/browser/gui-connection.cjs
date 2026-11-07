// GUI takeover/release only; one offline Demo Session keeps its existing loop.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {testDirectory,startServer,launchBrowser,cleanup,report}=require('./support.cjs');
(async()=>{
  const root=testDirectory('recoding-gui-connection-');let server,browser,result;const errors=[];
  const until=async predicate=>{const end=Date.now()+4000;while(!predicate()){assert.ok(Date.now()<end,'Timed out waiting for GUI release');await new Promise(resolve=>setTimeout(resolve,20));}};
  try{
    server=startServer(root);const base=await server.ready;browser=await launchBrowser();
    const context=await browser.newContext({viewport:{width:1450,height:950}});
    const a=await context.newPage();a.on('pageerror',e=>errors.push(e.message));
    await a.goto(base);await a.waitForFunction(()=>workspaceSync.loaded);
    await a.evaluate(()=>openSession({action:'create',name:'GUI keeps running'}));
    await a.waitForFunction(()=>state.session?.status==='sleeping'&&state.source!==null);
    const original=await a.evaluate(async()=>({page:state.pageId,document:state.documentId,csrf:state.config.csrf,
      instance_id:state.config.instance_id,id:state.session.id,run_id:(await api(`/api/sessions/${state.session.id}`)).run_id}));
    await a.reload();await a.waitForFunction(()=>state.session?.status==='sleeping'&&!state.switching);
    assert.equal(await a.evaluate(()=>state.pageId),original.page);
    assert.notEqual(await a.evaluate(()=>state.documentId),original.document);
    const late=await a.evaluate(async original=>{
      const {page,document,csrf,instance_id}=original;
      return (await fetch('/api/gui/release',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({page,document,csrf,instance_id})})).json();
    },original);
    assert.equal(late.released,false,'Late pagehide cannot release the refreshed document');
    const opened=context.waitForEvent('page');await a.evaluate(()=>window.open(location.href));
    const b=await opened;b.on('pageerror',e=>errors.push(e.message));
    await b.locator('#gui-connection-rejected[open]').waitFor();
    assert.equal(await b.locator('#takeover-gui').isEnabled(),true);
    await b.locator('#takeover-gui').click();await b.waitForFunction(()=>state.config&&!state.serverChanged&&workspaceSync.loaded);
    await a.locator('#gui-connection-rejected[open]').waitFor();
    assert.equal(await a.evaluate(()=>state.source),null,'Evicted page closes SSE and stops polling');
    await b.evaluate(id=>openSession({action:'browse',id}),original.id);
    await b.waitForFunction(()=>state.session?.status==='sleeping'&&!state.switching);
    assert.equal(await b.evaluate(()=>state.session.run_id),original.run_id,'Takeover does not restart the Agent loop');
    assert.equal(await a.evaluate(async()=>{
      return (await fetch('/api/heartbeat',{headers:{'X-recoding-Page':state.pageId,'X-recoding-Document':state.documentId}})).status;
    }),409,'An evicted page cannot reclaim with normal requests');
    await a.close({runBeforeUnload:true});
    assert.equal(await b.evaluate(async()=>Boolean(await api('/api/heartbeat'))),true,'Closing the evicted page cannot release its successor');
    const owner=await b.evaluate(()=>state.pageId);
    const logs=path.join(root,'.recoding','server-logs'),file=path.join(logs,fs.readdirSync(logs).find(name=>name.endsWith('.jsonl')));
    const released=()=>fs.readFileSync(file,'utf8').trim().split('\n').some(line=>{try{const e=JSON.parse(line);return e.event==='GUI_RELEASE'&&e.page===owner&&e.released;}catch{return false;}});
    // Native scripted-window close runs the browser lifecycle; CDP force-close
    // used by page.close() can discard the document without pagehide delivery.
    const closing=Date.now();await b.evaluate(()=>setTimeout(()=>window.close(),0));await until(released);
    const releaseMs=Date.now()-closing;
    const c=await browser.newPage({viewport:{width:1450,height:950}});c.on('pageerror',e=>errors.push(e.message));
    await c.goto(base);await c.waitForFunction(()=>state.config&&!state.serverChanged&&workspaceSync.loaded);
    await c.evaluate(id=>openSession({action:'browse',id}),original.id);
    await c.waitForFunction(()=>state.session?.status==='sleeping'&&!state.switching);
    assert.equal(await c.evaluate(()=>state.session.run_id),original.run_id,'Closing a GUI leaves its hosted Session running');
    assert.deepEqual(errors,[]);
    result={passed:true,checks:['Normal reload keeps page identity with a new document; late release ignored',
      'Blocked page offers explicit takeover; old SSE/API revoked without stopping Session',
      'Evicted page close cannot release current owner',
      'Normal page close sends release; next page connects without waiting 30s'],release_ms:releaseMs,
      external_model_api_calls:0,full_suite_run:false,errors};
  }finally{await cleanup({browser,server});}
  report(result);
})().catch(e=>{console.error(e);process.exitCode=1;});
