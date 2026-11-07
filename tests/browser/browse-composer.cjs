// Browse replaces the input surface; explicit Resume restores the retained draft.
const assert=require('node:assert/strict'),path=require('node:path');
const {execFileSync}=require('node:child_process');
const {project,testDirectory,environment,startServer,launchBrowser,cleanup,report}=require('./support.cjs');

(async()=>{
  const temp=testDirectory('recoding-browse-composer-'),root=path.join(temp,'workspace');
  const fixture=JSON.parse(execFileSync(path.join(project,'.venv/bin/python3'),['-c',
    'import json,sys; from session import SessionStore; s=SessionStore.create(sys.argv[1],"browse-input",{}); print(json.dumps(s.navigation_info()))',root],
    {cwd:project,env:environment(),encoding:'utf8'}));
  let server,browser;const errors=[],layouts=[];
  try{
    server=startServer(root);const base=await server.ready;browser=await launchBrowser();
    const page=await browser.newPage({viewport:{width:1500,height:950}});
    page.setDefaultTimeout(10000);page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base,{waitUntil:'domcontentloaded'});
    await page.waitForFunction(()=>workspaceSync.loaded&&!state.switching);
    const open=async()=>{
      assert(await page.evaluate(id=>openSession({action:'browse',id}),fixture.id));
      await page.waitForFunction(()=>state.agentId==='all'&&!state.switching);
    };
    await open();
    const notice=page.locator('#session-resume-notice'),prompt=page.locator('#prompt');
    assert.equal(await notice.isVisible(),true);
    assert.equal(await prompt.isDisabled(),true);
    assert.equal(await page.locator('#composer').evaluate(node=>node.inert),true);
    assert.equal(await page.locator('#send').isDisabled(),true);
    assert.equal(await page.locator('#composer-tips-button').count(),0);
    assert.equal(await page.locator('.composer-hint').textContent(),'任意时刻输入，Agent立即响应。');
    assert.equal(await page.locator('#stop').isDisabled(),true);
    const snapshot=await page.evaluate(id=>api(`/api/sessions/${id}`),fixture.id);
    assert.equal(snapshot.status,'stop');assert.equal(snapshot.read_only,true);
    assert.equal(snapshot.events.some(event=>event.type==='model_start'),false,'Browsing never starts an Agent');
    await prompt.evaluate(node=>node.focus());
    assert.equal(await prompt.evaluate(node=>node===document.activeElement),false,'Keyboard cannot focus the covered input');
    for(const width of [1500,700,390,320]){
      await page.setViewportSize({width,height:950});
      // A breakpoint animates the existing sidebar off-screen; inspect the settled surface.
      await page.waitForFunction(()=>{
        const form=document.getElementById('composer').getBoundingClientRect();
        return document.getElementById('session-resume-notice').contains(document.elementFromPoint(form.x+form.width/2,form.y+form.height/2));
      });
      const layout=await page.evaluate(()=>{
        const box=id=>document.getElementById(id).getBoundingClientRect(),form=box('composer'),notice=box('session-resume-notice'),button=box('continue-task');
        const hit=document.elementFromPoint(form.x+form.width/2,form.y+form.height/2);
        return {width:innerWidth,overflow:document.documentElement.scrollWidth>innerWidth,
          covered:notice.left<=form.left+1&&notice.right>=form.right-1&&notice.top<=form.top+1&&notice.bottom>=form.bottom-1,
          hitNotice:Boolean(hit?.closest('#session-resume-notice')),hit:hit ? `${hit.tagName}#${hit.id}.${hit.className}` : null,form:{x:form.x,y:form.y,width:form.width,height:form.height},notice:{x:notice.x,y:notice.y,width:notice.width,height:notice.height},
          toolbarFree:box('detail-visibility').bottom<=notice.top,
          buttonContained:button.left>=notice.left&&button.right<=notice.right&&button.bottom<=notice.bottom};
      });
      layouts.push(layout);
      assert.equal(layout.overflow,false,`Horizontal overflow at ${width}px`);
      assert.equal(layout.covered,true);assert.equal(layout.hitNotice,true);
      assert.equal(layout.toolbarFree,true);assert.equal(layout.buttonContained,true);
    }
    await page.setViewportSize({width:1500,height:950});
    const resume=async()=>{
      await page.locator('#continue-task').click();
      await page.waitForFunction(()=>state.session?.status==='sleeping'&&!state.session.read_only);
      assert.equal(await notice.isVisible(),false);assert.equal(await prompt.isEnabled(),true);
      assert.equal(await page.locator('#composer').evaluate(node=>node.inert),false);
    };
    await resume();
    const draft='Resume 后继续编辑的草稿';await prompt.fill(draft);
    const live=await page.evaluate(()=>({status:state.session.status,read_only:state.session.read_only}));
    for(const status of ['stopped','failed']){
      for(const width of [1500,390]){
        await page.setViewportSize({width,height:950});
        await page.evaluate(status=>{
          state.session.status=status;state.session.read_only=false;
          document.getElementById('prompt').value=['草稿保留','第二行','第三行'].join(String.fromCharCode(10));
          composerLayout.resize();controls();
        },status);
        assert.equal(await notice.isVisible(),true);
        assert.equal(await prompt.isDisabled(),true);
        assert.equal(await page.locator('#composer').evaluate(node=>node.inert),true);
        assert.equal(await page.locator('#send').isDisabled(),true);
        await page.waitForFunction(()=>{
          const form=document.getElementById('composer').getBoundingClientRect(),notice=document.getElementById('session-resume-notice');
          const rect=notice.getBoundingClientRect();
          return rect.left<=form.left+1&&rect.right>=form.right-1&&rect.top<=form.top+1&&rect.bottom>=form.bottom-1&&notice.contains(document.elementFromPoint(form.x+form.width/2,form.y+form.height/2));
        });
      }
    }
    await page.evaluate(live=>{Object.assign(state.session,live);controls();},live);
    await page.setViewportSize({width:1500,height:950});await prompt.fill(draft);
    await page.locator('#close-session').click();await page.waitForFunction(()=>!state.session);
    await open();
    assert.equal(await prompt.isDisabled(),true);assert.equal(await prompt.inputValue(),draft);
    await resume();assert.equal(await prompt.inputValue(),draft,'Browsing and Resume preserve the unsent local draft');
    assert.deepEqual(errors,[]);
    report({passed:true,checks:['Browse reminder covers the disabled, inert input; Resume remains usable',
      'Browse starts no Model calls; keyboard cannot enter the input',
      'Overlay stays within the input area at desktop and narrow widths',
      'Hosted stopped/failed Master also covers and disables the input, including multiline drafts',
      'Explicit Resume restores input and retains the local unsent draft'],layouts,external_model_api_calls:0,full_suite_run:false,errors});
  }catch(error){report({passed:false,error:error.stack,layouts,errors,external_model_api_calls:0,full_suite_run:false});throw error;}
  finally{await cleanup({browser,server});}
})().catch(error=>{console.error(error);process.exitCode=1;});
