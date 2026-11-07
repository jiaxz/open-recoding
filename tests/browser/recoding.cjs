// Reproducible recoding branding, archive sidebar and persistent workspace smoke test.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {testDirectory, startServer, launchBrowser, cleanup, report: writeReport} = require('./support.cjs');

(async()=>{
  const renameOnly=process.argv.includes('--rename-smoke');
  const composerOnly=process.argv.includes('--composer-smoke');
  const sidebarOnly=process.argv.includes('--sidebar-smoke'),smokeOnly=renameOnly||sidebarOnly||composerOnly;
  const temp=testDirectory('recoding-browser-');
  let server,browser,report;const checks=[],errors=[];
  try{
    server = startServer(temp);
    const base = await server.ready;
    browser = await launchBrowser();
    const page=await browser.newPage({viewport:{width:1512,height:1000}});
    page.on('pageerror',error=>errors.push(error.message));
    const ready=()=>page.waitForFunction(()=>state.session?.status==='ready'&&!state.switching);
    await page.goto(base,{waitUntil:'networkidle'});
    await page.evaluate(()=>{
      localStorage.setItem('recode-composer-layout',JSON.stringify({height:180,hidden:false}));
      localStorage.setItem('codexing-completion-sound','off');
    });
    await page.reload({waitUntil:'networkidle'});
    assert.equal(await page.title(),'recoding... · A Recursive Coding Agent');
    assert.match(await page.locator('.brand').innerText(),/recoding/);
    assert.equal(await page.locator('#history-path').count(),0);
    assert.equal(await page.locator('#inspector-panel .model-card').count(),1);
    assert.match(await page.locator('#inspector-panel').innerText(),/SESSION ARCHIVE/);
    assert.equal(await page.locator('.workspace-meta').count(),0);
    for(const id of ['network-state','gpu-state','workspace-path'])
      assert.equal(await page.locator(`#inspector-panel #${id}`).count(),1);
    assert.doesNotMatch(await page.locator('#inspector-panel').innerText(),/CURRENT TURN|SESSION HISTORY|Model transport/i);
    assert.equal(await page.locator('#model-name').isVisible(),true);
    assert.equal(await page.evaluate(()=>localStorage.getItem('recoding-completion-sound')),'off');
    assert.equal(await page.evaluate(()=>localStorage.getItem('codexing-completion-sound')),null);
    checks.push('Model/speed/runtime/workspace/archive sidebar without Current Turn, Session history or top runtime strip');

    if(composerOnly){
      assert.equal(await page.locator('.brand-mark use').getAttribute('href'),'#recoding-loop');
      assert.equal(await page.locator('#welcome').textContent(),'');
      assert.equal(await page.locator('.suggestions,.capabilities').count(),0);
      assert.equal(await page.locator('.brand-mark svg').evaluate(svg=>svg.getBBox().width>14&&svg.getBBox().height>14),true);
    }
    await page.locator('#new-session').click();
    const generatedName=await page.locator('#session-name').inputValue();
    if(composerOnly)assert.match(generatedName,/^session-\d{8}-\d{6}$/);
    else await page.locator('#session-name').fill('recoding workspace');
    const selectedName=composerOnly?generatedName:'recoding workspace';
    await page.locator('#session-submit').click();await ready();
    const original=await page.evaluate(()=>({id:state.session.id,path:state.session.session_directory}));
    assert(original.path.includes('/.recoding/'));
    assert.match(await page.locator('#session-store-path').innerText(),/\.recoding/);
    assert.equal(await page.locator('#copy-archive').isEnabled(),true);
    await page.locator('#prompt').fill('检查工作目录');await page.locator('#prompt').press('Enter');
    await page.waitForFunction(()=>document.querySelectorAll('.turn').length===1);await ready();
    assert.equal(await page.evaluate(()=>state.session.context.messages),5);
    assert.equal(await page.locator('#context-length').count(),0);
    assert.match(await page.locator('.agent-label').innerText(),/recoding/);
    assert.equal(await page.locator('.model-call').count(),2);
    assert.equal(await page.locator('.tool-call').count(),1);
    assert.equal(await page.locator('.final-response').last().isVisible(),true);
    assert.equal(await page.locator('.turn-totals-heading [data-metric=iterations]').innerText(),'2 Iterations');
    assert.equal(await page.locator('.turn-totals-heading [data-metric=models]').innerText(),'2 Model calls');
    assert.equal(await page.locator('.turn-totals-heading [data-metric=tools]').innerText(),'1 Tool calls');
    assert.equal(await page.locator('#inspector-session').innerText(),selectedName);
    assert.equal(await page.locator('#workspace-path').innerText(),temp);
    assert.equal(await page.locator('#network-state').innerText(),'off');
    assert.equal(await page.locator('#gpu-state').innerText(),'off');
    checks.push('New Session archive path, real Python worker/tool execution and Model call display; Context metadata stays available without a composer statistics row');

    if(composerOnly){
      assert.equal(await page.locator('.agent-label use').getAttribute('href'),'#recoding-loop');
      assert.equal(await page.locator('[data-preference],.enhancement-bar').count(),0);
      assert.equal(await page.locator('#composer-help').isVisible(),false);
      for(const viewport of [{width:1512,height:1000},{width:1024,height:768},{width:390,height:800},{width:320,height:740}]){
        await page.setViewportSize(viewport);
        await page.waitForFunction(()=>document.documentElement.scrollWidth<=innerWidth);
        const send=await page.locator('#send').boundingBox();assert(send);
        await page.locator('#composer-help-button').click();
        await page.waitForFunction(()=>document.querySelector('#composer-help').matches(':popover-open'));
        assert.match(await page.locator('#composer-help').innerText(),/输入帮助/);
        const help=await page.locator('#composer-help').boundingBox();
        assert(help.x>=0&&help.y>=0&&help.x+help.width<=viewport.width&&help.y+help.height<=viewport.height,'Help must fit within the viewport');
        const helpButton=await page.locator('#composer-help-button').boundingBox();
        assert(Math.abs(help.x-Math.max(12,Math.min(helpButton.x,viewport.width-help.width-12)))<2,`Help must open beside its question mark, without an initial corner flash: ${JSON.stringify({help,helpButton,viewport})}`);
        await page.keyboard.press('Escape');assert.equal(await page.locator('#composer-help').isVisible(),false);
      }
      await page.locator('#composer-help-button').focus();await page.keyboard.press('Enter');
      await page.waitForFunction(()=>document.querySelector('#composer-help').matches(':popover-open'));
      await page.getByRole('button',{name:'关闭输入帮助',exact:true}).click();
      assert.equal(await page.locator('#composer-help').isVisible(),false);
      await page.locator('#composer-help-button').click();await page.mouse.click(2,2);
      assert.equal(await page.locator('#composer-help').isVisible(),false);
      const fallback=await page.evaluate(async directory=>{
        const session=await api('/api/sessions',{action:'create',directory});
        await api(`/api/sessions/${session.id}/close`,{});return session.name;
      },temp);
      assert.match(fallback,/^session-\d{8}-\d{6}$/);
      await page.setViewportSize({width:1512,height:1000});
      await page.locator('#prompt').fill('等待');await page.locator('#prompt').press('Enter');
      await page.waitForFunction(()=>document.querySelector('.tool-call[data-status=running]'));
      let cancelRequests=0;page.on('request',request=>{if(request.url().endsWith('/cancel'))cancelRequests++;});
      await page.locator('#composer-help-button').click();await page.keyboard.press('Escape');
      assert.equal(await page.locator('#composer-help').isVisible(),false);
      assert.equal(await page.evaluate(()=>state.session.status),'running');assert.equal(cancelRequests,0);
      await page.locator('#stop').click();await ready();
      if(process.env.AGENT_UI_SCREENSHOT){await page.locator('#composer-help-button').click();await page.screenshot({path:process.env.AGENT_UI_SCREENSHOT});}
      checks.push('Compact persistent switches share the Send row at 1512/1024/390/320px; readable default names work in UI and backend; iterative loop marks render');
      checks.push('Composer input help fits narrow screens; click, keyboard and outside dismissal work; Escape during a running tool only dismisses help');
    }

    if(!smokeOnly){
    if(await page.locator('.steps').isVisible())await page.locator('.turn-toggle').click();
    assert.equal(await page.locator('.steps').isVisible(),false);
    await page.locator('#prompt').fill('保留草稿');await page.locator('#toggle-composer').click();
    assert.equal(await page.locator('#composer-body').isVisible(),false);
    assert.equal(await page.locator('#context-length').count(),0);
    await page.locator('#toggle-composer').click();assert.equal(await page.locator('#prompt').inputValue(),'保留草稿');
    checks.push('Turn folding and input hide/show remain usable with the new layout');
    }


    if(!smokeOnly){
    for(const viewport of [{width:1512,height:1000},{width:1024,height:768},{width:390,height:700}]){
      await page.setViewportSize(viewport);
      await page.waitForFunction(()=>document.documentElement.scrollWidth<=innerWidth&&document.documentElement.scrollHeight<=innerHeight);
      if(viewport.width===390){
        await page.locator('#toggle-inspector').click();assert.equal(await page.locator('#session-store-path').isVisible(),true);
        await page.locator('#hide-inspector').click();
      }
    }
    await page.setViewportSize({width:1512,height:1000});
    if(process.env.AGENT_UI_SCREENSHOT)await page.screenshot({path:process.env.AGENT_UI_SCREENSHOT});
    checks.push('Desktop, tablet and phone layouts fit the viewport; archive drawer opens independently');
    }
    assert.deepEqual(errors,[]);
    report={passed:true,checks,errors,rename_only:renameOnly,sidebar_only:sidebarOnly,composer_only:composerOnly,full_suite_run:false,external_model_api_calls:0};
  }finally{
    await cleanup({browser, server, temp});
  }
  writeReport(report);
})().catch(error=>{console.error(error);process.exitCode=1;});
