// Focused real discovery startup and tool-completeness sidebar states.
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {execFileSync}=require('node:child_process');
const {testDirectory, startServer,launchBrowser,cleanup,report,project,environment}=require('./support.cjs');
(async()=>{
  const temp=testDirectory('recoding-tools-');
  let server,browser,result;const errors=[],refreshPaths=[];
  try{
    server=startServer(temp);const base=await server.ready;browser=await launchBrowser();
    const page=await browser.newPage({viewport:{width:1400,height:950}});page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base,{waitUntil:'domcontentloaded'});await page.waitForFunction(()=>state.config&&workspaceSync.loaded);
    assert.equal(await page.locator('#exit-host').count(),0,'Exit application button was removed');
    await page.locator('#new-session').click();await page.locator('#session-submit').click();
    await page.waitForFunction(()=>state.session?.tool_inventory?.status==='ready'&&!state.switching);
    assert.equal(await page.evaluate(()=>state.agentId),'all');
    assert.equal(await page.locator('#tool-inventory').isVisible(),true,'Default All agents view must show tool inventory');
    assert.equal(await page.locator('#tool-inventory-agent').innerText(),'Master');
    const actual=await page.evaluate(()=>state.session.tool_inventory);
    assert(actual.score>=0&&actual.score<=100);assert.equal(actual.python,'/usr/bin/python3');assert.equal(actual.interval_seconds,300);
    assert.equal(actual.scope,'shared_sandbox_fresh_python_discovery');
    assert(fs.existsSync(path.join(await page.evaluate(()=>state.session.session_directory),'tool-inventory.json')));
    const score=page.locator('.inventory-score'),content=page.locator('#tool-inventory-content');
    const fixtures=JSON.parse(execFileSync(path.join(project,'.venv','bin','python3'),['-c',
      'import json,time; from node.inventory import evaluate; from test_tool_inventory import discovery; d=discovery(True); good=evaluate(d); [(d["modules"][key].update(found=False)) for key in ["numpy","pandas","polars","scipy","sympy","openpyxl","pyarrow"]]; partial=evaluate(d); bad=evaluate(discovery()); values={"good":good,"partial":partial,"bad":bad}; [value.update(checked_at=time.time()+1,packages=[{"name":"fixture-package","version":"1.2.3","scope":"workspace"}],package_count=1,startup_hooks_skipped=True) for value in values.values()]; print(json.dumps(values))'],
      {cwd:project,env:environment({PYTHONPATH:'tests'}),encoding:'utf8'}));
    const show=report=>page.evaluate(value=>{state.session.tool_inventory=value;inspector();},report);
    await show(fixtures.good);assert.equal(await score.innerText(),'100/100');
    assert.equal(await content.getAttribute('data-level'),'good');
    await page.locator('.inventory-details > summary').click();
    assert.equal(await page.locator('.inventory-category').count(),6);
    await page.locator('.inventory-packages > summary').click();
    assert.match(await page.locator('.inventory-package-list').innerText(),/fixture-package 1\.2\.3 · workspace/);
    assert.match(await content.innerText(),/未执行 .pth/);
    await show(fixtures.partial);assert.equal(await score.innerText(),'75/100');
    assert.equal(await page.locator('.inventory-warning,.inventory-host-advice').count(),0);
    await show(fixtures.bad);assert.equal(await score.innerText(),'10/100');
    assert.equal(await page.locator('.inventory-warning').isVisible(),true);
    assert.match(await page.locator('.inventory-host-advice').innerText(),/Host[\s\S]*\/usr\/bin\/python3[\s\S]*numpy/);
    assert.match(await page.locator('.inventory-host-advice').innerText(),/Session 自行安装仍然可用/);
    await show({...fixtures.bad,checked_at:Date.now()/1000-60});
    await page.route('**/tool-inventory',async route=>{refreshPaths.push(new URL(route.request().url()).pathname);await route.fulfill({status:202,json:{queued:true}});});
    await page.locator('#refresh-tools').click();assert.match(refreshPaths[0],/^\/api\/sessions\/[^/]+\/tool-inventory$/);
    await page.evaluate(()=>selectAgent('master'));
    assert.equal(await page.locator('#refresh-tools').isDisabled(),true,'All agents and Master share the same refresh state');
    assert.equal(await page.locator('#tool-inventory').isVisible(),true);
    await show({...fixtures.good,checked_at:Date.now()/1000+2});assert.equal(await page.locator('#refresh-tools').isDisabled(),false);
    await page.evaluate(value=>{
      renderAgentEvent({type:'tool_inventory',sequence:state.viewSequence+1,run_id:'inventory-only',inventory:value},true);
    },fixtures.good);
    assert.equal(await page.locator('.agent-run[data-run-id=inventory-only]').count(),0,'Telemetry cannot create Agent-loop detail cards');
    await page.evaluate(value=>{
      const id='d'.repeat(32);state.session.agents=[{id,name:'Paused peer',status:'stopped',created_at:Date.now()/1000}];
      installChild({id,name:'Paused peer',status:'stopped',events:[],sequence:0,tool_inventory:value},id);
    },fixtures.partial);
    assert.equal(await score.innerText(),'75/100','Current Agent uses its own inventory, not Master inventory');
    assert.equal(await page.locator('#tool-inventory-agent').innerText(),'Paused peer');
    assert.equal(await page.locator('#refresh-tools').isDisabled(),true);assert.match(await page.locator('.inventory-checked').innerText(),/Agent 已停止/);
    await page.evaluate(()=>{state.agentDetail.tool_inventory={status:'error',error:'fixture timeout'};inspector();});
    assert.equal(await score.count(),0);assert.match(await content.innerText(),/暂不评分/);
    assert.equal(await page.locator('.inventory-host-advice').count(),0,'A probe error never claims missing host packages');
    await page.evaluate(()=>{state.agentDetail.tool_inventory=null;inspector();});
    assert.match(await content.innerText(),/尚无检查记录.*Resume/);
    assert.deepEqual(errors,[]);result={passed:true,checks:[
      'All agents shows Master inventory, single-Agent views show their own report, and the exit application button is absent',
      'Actual worker publishes and saves an initial inventory without model API calls',
      'Compact sidebar shows weighted score, categorized coverage, package versions/scopes and severe Host advice',
      'Refresh routes to the current Agent; stopped Agents retain cached results and cannot start scans',
      'Telemetry does not create loop cards; Agent inventories stay separate; probe errors are not scored as zero'
    ],external_model_api_calls:0,full_suite_run:false,errors};
  }finally{await cleanup({browser,server,temp});}
  report(result);
})().catch(error=>{console.error(error);process.exitCode=1;});
