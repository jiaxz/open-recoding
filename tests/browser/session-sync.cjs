// Focused sync-status test with a real Demo server and controlled HTTP delays.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {spawnSync,execFileSync} = require('node:child_process');
const {testDirectory, project, environment, startServer, launchBrowser, cleanup, report: writeReport} = require('./support.cjs');

(async()=>{
const temp=testDirectory('agent-session-sync-');
  const root=path.join(temp,'workspace');
  const seeded=spawnSync('python3',['-c',"from session.store import SessionStore;import sys;SessionStore.create(sys.argv[1],'Saved session',{})",root],{cwd:project,encoding:'utf8'});
  assert.equal(seeded.status,0,seeded.stderr);
  let server,browser,report;
  const checks=[],errors=[];
  try{
    execFileSync('python3',['-m','unittest','test_startup_progress.StartupProgressTests.test_live_progress_does_not_wait_for_scan_or_resume_lock'],
      {cwd:project,env:environment({PYTHONPATH:path.join(project,'tests')}),stdio:'pipe'});
    checks.push('Real progress HTTP requests stay responsive while scan/Resume hold the manager lock; worker startup stages and completed timings are reported');
    server = startServer(root);
    const base = await server.ready;
    browser = await launchBrowser();
    const page=await browser.newPage({viewport:{width:1400,height:900}});
    page.on('pageerror',error=>errors.push(error.message));
    let release,gate=new Promise(resolve=>{release=resolve;}),fail=false,resumeGate,releaseResume;
    let phase='加载 Session 对话与工具历史';
    await page.route('**/api/progress?*',async route=>{
      if(!gate&&!resumeGate){await route.continue();return;}
      // UI delay fixture; the Python check above exercises actual blocked I/O.
      await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({operation:{status:'running',steps:[
        {key:'lock',label:'等待 Session 管理器',elapsed_seconds:.1,status:'done'},
        {key:resumeGate?'history':'scan',label:resumeGate?phase:'扫描工作目录',detail:root,elapsed_seconds:.2,status:'running'}]}})});
    });
    await page.route('**/api/sessions',async route=>{
      if(route.request().method()==='POST'){if(resumeGate)await resumeGate;await route.continue();return;}
      if(fail){await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'fixture sync failure'})});return;}
      const pending=gate;if(pending)await pending;
      await route.continue();
    });
    const banner=page.locator('#workspace-sync');
    const done=()=>page.waitForFunction(()=>document.getElementById('workspace-sync').dataset.state==='done');
    await page.goto(base,{waitUntil:'domcontentloaded'});
    await page.waitForFunction(root=>document.getElementById('workspace-sync-path').textContent===root,root);
    assert.equal(await banner.isVisible(),true);
    assert.equal(await banner.getAttribute('data-state'),'syncing');
    assert.match(await page.locator('#workspace-sync-label').innerText(),/正在同步当前工作目录/);
    assert.equal(await page.locator('#session-list').getAttribute('aria-busy'),'true');
    assert.equal(await page.locator('.session-empty').count(),0,'Do not show a false empty result while synchronizing');
    await page.waitForFunction(()=>document.getElementById('workspace-sync-elapsed').textContent.endsWith('s'));
    await page.waitForFunction(()=>document.getElementById('workspace-sync-stage').textContent.includes('扫描工作目录'));
    assert.match(await page.locator('#workspace-sync-steps').textContent(),/等待 Session 管理器/);
    gate=null;release();await done();
    assert.match(await page.locator('#workspace-sync-label').innerText(),/已同步.*1 Session/);
    assert.equal(await page.locator('.session-row').count(),1);
    assert.equal(await page.locator('#session-list').getAttribute('aria-busy'),'false');
    await banner.waitFor({state:'hidden'});
    checks.push('First load shows real workspace path, spinner/elapsed and busy state until the catalog arrives, then completion/count and auto-hide');

    gate=new Promise(resolve=>{release=resolve;});
    await page.evaluate(()=>{listSessions().catch(()=>{});});
    await banner.waitFor({state:'visible'});
    assert.equal(await banner.getAttribute('data-state'),'syncing');
    assert.equal(await page.locator('.session-row').count(),1);
    gate=null;release();await done();
    checks.push('A slow subsequent sync shows status without clearing the existing Session list');

    fail=true;
    await page.evaluate(()=>listSessions().catch(()=>{}));
    assert.equal(await banner.getAttribute('data-state'),'error');
    assert.match(await page.locator('#workspace-sync-detail').innerText(),/fixture sync failure/);
    assert.equal(await page.locator('.session-row').count(),1);
    fail=false;gate=new Promise(resolve=>{release=resolve;});
    await page.locator('#retry-workspace-sync').click();
    assert.equal(await banner.getAttribute('data-state'),'syncing');
    gate=null;release();await done();
    assert.equal(await page.locator('#retry-workspace-sync').isVisible(),false);
    checks.push('The banner displays the current server phase, per-phase elapsed time and completed phases; background sync failure/retry keeps the list intact');

    resumeGate=new Promise(resolve=>{releaseResume=resolve;});
    await page.locator('.session-row').first().click();
    await page.waitForFunction(()=>document.getElementById('workspace-sync-stage').textContent.includes('加载 Session 对话'));
    assert.match(await page.locator('#workspace-sync-label').innerText(),/恢复 Session/);
    phase='加载历史测速数据';
    await page.waitForFunction(()=>document.getElementById('workspace-sync-stage').textContent.includes('加载历史测速数据'));
    assert.equal(await banner.getAttribute('data-state'),'syncing');
    resumeGate=null;releaseResume();
    await page.waitForFunction(()=>state.session?.status==='ready'&&!state.switching);await done();
    assert.match(await page.locator('#workspace-sync-label').innerText(),/Session 已就绪/);
    assert.match(await page.locator('#workspace-sync-steps').textContent(),/渲染 Session 历史/);
    checks.push('Manual Resume displays changing backend phases, paints history-render progress and remains visible through worker readiness; background polling does not replace it');

    resumeGate=new Promise(resolve=>{releaseResume=resolve;});phase='恢复 Agent 对话与历史图片';
    await page.reload({waitUntil:'domcontentloaded'});
    await page.waitForFunction(()=>document.getElementById('workspace-sync-stage').textContent.includes('恢复 Agent 对话与历史图片'));
    assert.equal(await banner.isVisible(),true);
    assert.equal(await banner.getAttribute('data-state'),'syncing');
    resumeGate=null;releaseResume();
    await page.waitForFunction(()=>state.session?.status==='ready'&&!state.switching);await done();
    assert.match(await page.locator('#workspace-sync-label').innerText(),/Session 已就绪/);
    assert.match(await page.locator('#workspace-sync-steps').textContent(),/连接后端并读取配置/);
    checks.push('Automatic Resume after reload stays in the same visible startup banner instead of reporting completion immediately after directory synchronization');
    assert.deepEqual(errors,[]);
    report={passed:true,checks,errors,external_model_api_calls:0,full_suite_run:false};
  }finally{
    await cleanup({browser, server, temp});
  }
  writeReport(report);
})().catch(error=>{console.error(error);process.exitCode=1;});
