// Focused restart transport, local directory folding and compact navigation check.
const assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),net=require('node:net');
const {execFileSync}=require('node:child_process');
const {testDirectory, project,environment,startServer,launchBrowser,cleanup,report:writeReport}=require('./support.cjs');

(async()=>{
  const temp=testDirectory('recoding-restart-navigation-');
  let server,browser;const sockets=[],checks=[],errors=[];let report;
  try {
    const seeded=JSON.parse(execFileSync('python3',['-c',
      "import json,sys;from pathlib import Path;from session.store import SessionStore;r=Path(sys.argv[1]);print(json.dumps([SessionStore.create(r/p,n,{}).navigation_info() for p,n in [('.', 'Root'),('child','Child'),('child/grandchild','Grandchild')]]))",temp],
      {cwd:project,env:environment(),encoding:'utf8'}));
    server=startServer(temp);const base=await server.ready,port=Number(new URL(base).port);
    browser=await launchBrowser();const context=await browser.newContext();
    const old=await context.newPage();old.on('pageerror',error=>errors.push(error.message));
    await old.goto(base,{waitUntil:'domcontentloaded'});
    const ready=page=>page.waitForFunction(()=>state.session?.status==='ready'&&!state.switching);
    const row=(page,id)=>page.locator(`.session-row[data-id="${id}"] .session-item`);
    const group=(page,directory)=>page.locator('.workspace-group').filter({has:page.locator(`:scope > summary > span[title="${directory}"]`)});
    await row(old,seeded[0].id).click();await ready(old);
    const previous=await old.evaluate(()=>state.config.instance_id);

    // Abrupt replacement exercises stale EventSource clients without a final event.
    const stopped=new Promise(resolve=>server.process.once('exit',resolve));server.process.kill('SIGKILL');await stopped;
    server=startServer(temp,{port});await server.ready;
    await old.evaluate(()=>verifyServiceInstance());
    await old.waitForFunction(()=>state.serverChanged && state.source===null);
    assert.match(await old.locator('#workspace-sync-label').innerText(),/Web 服务已重启/);
    assert.equal(await old.locator('#retry-workspace-sync').innerText(),'重新连接');
    let staleRequests=0;old.on('request',request=>{if(new URL(request.url()).pathname.startsWith('/api/'))staleRequests++;});
    await old.evaluate(()=>{scheduleNavigationRefresh();document.dispatchEvent(new Event('visibilitychange'));});
    await new Promise(resolve=>setTimeout(resolve,2200));assert.equal(staleRequests,0,'Replaced pages must stop polling/reconnecting');

    // Burst-connect while the stale page remains open. Old backlog=5 took ~1 s.
    const connect=await Promise.all(Array.from({length:48},()=>new Promise((resolve,reject)=>{
      const started=Date.now(),socket=net.createConnection({host:'127.0.0.1',port});sockets.push(socket);
      socket.once('connect',()=>resolve(Date.now()-started));socket.once('error',reject);
      socket.setTimeout(3000,()=>reject(new Error('connect timeout')));
    })));
    for(const socket of sockets)socket.destroy();sockets.length=0;
    assert(Math.max(...connect)<900,`Burst connections must avoid the 1 s retransmission cliff: ${Math.max(...connect)} ms`);
    const page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));
    const started=Date.now();await page.goto(base,{waitUntil:'domcontentloaded'});await ready(page);
    const openMs=Date.now()-started;
    assert.notEqual(await page.evaluate(()=>state.config.instance_id),previous);
    assert(openMs<3000,`A fresh page with the old page retained took ${openMs} ms`);
    const obsolete=await page.request.get(`${base}/api/sessions/${seeded[0].id}/events?instance=${previous}`);
    assert.equal(obsolete.status(),204);
    checks.push(`Old page stops after server replacement; stale SSE gets 204; 48 TCP connections max ${Math.max(...connect)} ms; new page ready in ${openMs} ms with old page retained`);

    // A hidden page releases its SSE socket, then reconnects from saved sequence.
    await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'));});
    assert.equal(await page.evaluate(()=>state.source),null);
    await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new Event('visibilitychange'));});
    await page.waitForFunction(()=>state.source?.readyState===EventSource.OPEN);
    checks.push('Hidden pages release realtime connections; returning to the page verifies the server and resumes event delivery');

    let release;const gate=new Promise(resolve=>{release=resolve;});
    await page.route('**/api/sessions',async route=>{if(route.request().method()==='POST')await gate;await route.continue();});
    await row(page,seeded[1].id).click();
    await page.waitForFunction(()=>state.switching);
    assert.equal(await page.locator('#workspace-sync').getAttribute('data-mode'),'session');
    assert.equal(await page.locator('#workspace-sync-path').isVisible(),false);
    assert.equal(await page.locator('#workspace-sync-timing').getAttribute('open'),null);
    release();await ready(page);await page.unroute('**/api/sessions');
    assert((await page.locator('#workspace-sync').boundingBox()).height<85,'Session status stays a compact summary');
    checks.push('Opening a Session shows a compact summary, with its path and per-stage timing list hidden by default');

    const parent=group(page,temp),child=group(page,path.join(temp,'child'));
    await parent.locator(':scope > summary').click();
    assert.equal(await parent.getAttribute('open'),null);
    assert.equal(await row(page,seeded[0].id).isVisible(),false);
    assert.equal(await row(page,seeded[1].id).isVisible(),true);
    assert.equal(await row(page,seeded[2].id).isVisible(),true);
    await child.locator(':scope > summary').click();
    assert.equal(await row(page,seeded[1].id).isVisible(),false);
    assert.equal(await row(page,seeded[2].id).isVisible(),true);
    await page.evaluate(()=>listSessions());
    await row(page,seeded[2].id).click();await ready(page);
    assert.equal(await parent.getAttribute('open'),null);
    assert.equal(await child.getAttribute('open'),null);
    assert.equal(await row(page,seeded[2].id).isVisible(),true);
    checks.push('Collapsing a directory hides only its own Sessions; descendants keep their independent display state across refresh and selection');

    await page.evaluate(()=>{
      state.session.agents=Array.from({length:12},(_,i)=>({id:`fake-${i}`,name:'很长的子任务名称用于检查截断显示 '+i,
        status:i===11?'failed':'running',created_at:i,model:'gemini-4-argon',task:{task:'Long task summary retained in hover'}}));
      state.agentId='fake-11';renderTeam();
    });
    for(const viewport of [{width:1450,height:950},{width:390,height:850},{width:320,height:740}]){
      await page.setViewportSize(viewport);await page.evaluate(()=>layoutAgentTabs());
      assert.equal(await page.locator('.agent-tab[data-agent-id=fake-11]').isVisible(),true);
      const geometry=await page.locator('.agent-tab:visible').evaluateAll(tabs=>tabs.map(tab=>{
        const box=tab.getBoundingClientRect(),name=tab.querySelector('strong').getBoundingClientRect(),status=tab.querySelector('.agent-tab-status').getBoundingClientRect();
        return {width:box.width,height:box.height,aligned:Math.abs(name.y+name.height/2-status.y-status.height/2)<4,brief:getComputedStyle(tab.querySelector('.agent-tab-brief')).display};
      }));
      assert(geometry.every(tab=>tab.height<=23 && tab.width<=161 && tab.aligned && tab.brief==='none'));
      assert(await page.locator('.agent-sub-tabs').evaluate(node=>node.scrollWidth<=node.clientWidth+1));
      assert(await page.locator('.agent-sub-tabs').evaluate(node=>node.clientHeight<=48&&node.scrollHeight>node.clientHeight));
      assert.equal(await page.locator('#agent-tabs-more,#agent-tab-overflow').count(),0);
    }
    assert.deepEqual(errors,[]);
    checks.push('Master/current/live Tabs use one compact horizontal line, capped widths and ellipsis at desktop/390/320 px; two-row overflow still uses the dropdown without horizontal scrolling');
    report={passed:true,checks,errors,external_model_api_calls:0,full_suite_run:false};
  }finally{for(const socket of sockets)socket.destroy();await cleanup({browser,server,temp});}
  writeReport(report);
})().catch(error=>{console.error(error);process.exitCode=1;});
