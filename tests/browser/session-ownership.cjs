// Two GUI Hosts share stopped archives; one Host owns run and revokes other browsing.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {execFileSync}=require('node:child_process');
const {project,testDirectory,environment,startServer,launchBrowser,cleanup,stopServer,report}=require('./support.cjs');

(async()=>{
  const temp=testDirectory('recoding-host-protocol-'),workspace=path.join(temp,'workspace');
  const fixture=JSON.parse(execFileSync(path.join(project,'.venv/bin/python3'),['-c',
    'import json,sys;from pathlib import Path;from session import SessionStore;s=SessionStore.create(sys.argv[1],"shared-history",{});image=s.add_upload(Path(sys.argv[2]).read_bytes(),"sample.jpg");print(json.dumps({**s.info(),"image":image}))',
    workspace,path.join(project,'tests/fixtures/small.jpg')],{cwd:project,env:environment(),encoding:'utf8'}));
  let first,second,browser,result;const errors=[],logs=[];
  try{
    first=startServer(workspace);second=startServer(workspace);
    first.process.stdout.on('data',chunk=>logs.push(chunk.toString()));
    const [baseA,baseB]=await Promise.all([first.ready,second.ready]);browser=await launchBrowser();
    const contextA=await browser.newContext({viewport:{width:1450,height:950}});
    const a=await contextA.newPage(),b=await browser.newPage({viewport:{width:1450,height:950}});
    for(const [page,base] of [[a,baseA],[b,baseB]]){
      page.on('pageerror',error=>errors.push(error.message));
      await page.goto(base,{waitUntil:'domcontentloaded'});await page.waitForFunction(()=>workspaceSync.loaded);
    }
    const select=async page=>{
      await page.evaluate(()=>listSessions());
      await page.locator(`.directory-sessions .session-row[data-id="${fixture.id}"] .session-item`).click();
      await page.waitForFunction(id=>state.session?.id===id&&state.agentId==='all'&&!state.switching,fixture.id);
    };
    await select(a);await select(b);
    for(const page of [a,b]){
      assert.equal(await page.locator('#agent-tabs > button').first().getAttribute('data-agent-id'),'all');
      assert.equal(await page.evaluate(()=>state.session.read_only),true);assert.equal(await page.evaluate(()=>state.source),null);
    }
    const rejected=await a.context().newPage();
    const inherited=await a.evaluate(()=>sessionStorage.getItem('recoding-page-id'));
    await rejected.addInitScript(id=>sessionStorage.setItem('recoding-page-id',id),inherited);
    await rejected.goto(baseA,{waitUntil:'domcontentloaded'});
    await rejected.locator('#gui-connection-rejected[open]').waitFor();
    assert.equal(await rejected.evaluate(()=>state.config),null,'Second GUI cannot attach to the same Host');
    assert.notEqual(await rejected.evaluate(()=>state.pageId),inherited,'A duplicated tab cannot impersonate a still-open GUI');
    await rejected.close();
    const metadata=path.join(fixture.session_directory,'session.json'),original=fs.readFileSync(metadata,'utf8');
    await a.locator('#toggle-agent-settings').click();
    await a.locator('#agent-limit').fill('9');await a.locator('#agent-limit').press('Tab');
    assert.equal(fs.readFileSync(metadata,'utf8'),original);
    await a.locator('#continue-task').click();await a.waitForFunction(()=>state.session?.session_state==='run'&&state.session.status==='sleeping');
    assert.equal(JSON.parse(fs.readFileSync(metadata)).settings.max_subagents,9);
    await b.waitForFunction(()=>state.session===null);
    const pageA=await a.evaluate(()=>state.pageId),pageB=await b.evaluate(()=>state.pageId);
    assert.equal((await a.request.get(`${baseA}/api/sessions/${fixture.id}/images/${fixture.image.sha256}?page=${pageA}`)).status(),200);
    assert.equal((await b.request.get(`${baseB}/api/sessions/${fixture.id}/images/${fixture.image.sha256}?page=${pageB}`)).status(),409);
    const denied=await b.evaluate(async id=>{try{await api(`/api/sessions/${id}`,undefined);return false;}catch(e){return e.status;}},fixture.id);
    assert.equal(denied,409);
    await a.reload({waitUntil:'domcontentloaded'});await a.waitForFunction(()=>state.session?.session_state==='run'&&!state.switching);
    assert.equal(await a.evaluate(()=>state.pageId),pageA,'Refresh reconnects GUI without dropping Session ownership');
    // One GUI Host can hold multiple Sessions; switching is independent of hosting.
    await a.locator('#new-session').click();await a.locator('#session-name').fill('second-live');await a.locator('#session-submit').click();
    await a.waitForFunction(()=>state.session?.name==='second-live'&&state.session.status==='sleeping');
    const busy=await b.evaluate(async id=>(await api('/api/sessions')).sessions.find(s=>s.id===id).busy,fixture.id);
    assert.equal(busy,true);
    await select(a);await a.locator('#stop').click();await a.waitForFunction(()=>state.session?.read_only);
    await select(b);await b.locator('#continue-task').click();await b.waitForFunction(()=>state.session?.session_state==='run');
    await a.waitForFunction(()=>state.session===null);
    await stopServer(first);
    assert.equal(first.process.exitCode,0,'Exit GUI Host closes its remaining hosted Session');
    await b.locator('#stop').click();await b.waitForFunction(()=>state.session?.read_only);
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(temp,'first-host.log'),logs.join(''));
    result={passed:true,checks:['Two different Host processes may browse the same stopped archive',
      'A second GUI page on the same Host is rejected', 'Resume applies staged settings and revokes other Host browsing, including image endpoints',
      'GUI reload preserves its hosted Sessions', 'One GUI Host holds multiple Sessions across view switching',
      'Stop releases ownership for another Host', 'Ending the GUI Host process stops all Sessions it hosts'],
      external_model_api_calls:0,full_suite_run:false,errors};
  }finally{await cleanup({browser,server:first});await stopServer(second);}
  report(result);
})().catch(error=>{console.error(error);process.exitCode=1;});
