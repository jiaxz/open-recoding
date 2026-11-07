// Focused Runtime removal, non-Session directory filters and compaction display.
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {execFileSync}=require('node:child_process');
const {testDirectory, startServer,launchBrowser,cleanup,report,project,environment}=require('./support.cjs');
(async()=>{
  const temp=testDirectory('recoding-context-folders-');
  let server,browser,result;const errors=[];
  try{
    const parent=path.join(temp,'projects'),leaf=path.join(parent,'middle','work'),empty=path.join(parent,'empty');
    const ordinary=path.join(temp,'ordinary'),ordinaryChild=path.join(ordinary,'child');
    fs.mkdirSync(empty,{recursive:true});fs.mkdirSync(ordinaryChild,{recursive:true});
    const id=execFileSync('python3',['-c','import sys; from session.store import SessionStore; print(SessionStore.create(sys.argv[1],"known",{}).id)',leaf],{cwd:project,env:environment(),encoding:'utf8'}).trim();
    server=startServer(temp);const base=await server.ready;browser=await launchBrowser();
    const page=await browser.newPage({viewport:{width:1400,height:950}});page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base,{waitUntil:'domcontentloaded'});await page.waitForFunction(()=>state.config&&workspaceSync.loaded);
    const folder=directory=>page.locator(`.workspace-group[data-directory="${directory}"]`);
    const toggle=directory=>folder(directory).locator(':scope > .non-session-toggle');
    assert.equal(await folder(ordinary).isVisible(),true,'All root-level folders remain visible');
    assert.equal(await folder(parent).getAttribute('open'),'');assert.equal(await folder(leaf).isVisible(),true);
    assert.equal(await folder(empty).count(),0,'Deep non-Session siblings start hidden');
    assert.equal(await toggle(temp).count(),0,'Workspace root is exempt from filtering');
    assert.match(await toggle(parent).innerText(),/^Display non-session folders/);
    await toggle(parent).click();assert.equal(await folder(empty).isVisible(),true);assert.match(await toggle(parent).innerText(),/^Hide non-session folders/);
    await page.evaluate(()=>listSessions());assert.equal(await folder(empty).isVisible(),true,'Refresh retains the per-directory display choice');
    await toggle(parent).click();assert.equal(await folder(empty).count(),0);assert.equal(await folder(leaf).isVisible(),true);
    await folder(ordinary).locator(':scope > summary').click();await toggle(ordinary).waitFor();
    assert.equal(await folder(ordinaryChild).count(),0);await toggle(ordinary).click();assert.equal(await folder(ordinaryChild).isVisible(),true);
    await folder(ordinaryChild).locator(':scope > summary > .workspace-add').click();
    assert.equal(await page.locator('#session-directory').inputValue(),ordinaryChild);await page.locator('#dismiss-dialog').click();
    await page.locator(`.session-row[data-id="${id}"] .session-item`).click();await page.waitForFunction(()=>state.session?.status==='sleeping'&&!state.switching);
    assert.equal(await page.locator('#inspector-session,#network-state,#gpu-state,.runtime-row').count(),0);
    assert.equal(await page.locator('#workspace-path').isVisible(),true);assert.equal(await page.locator('#session-store-path').isVisible(),true);
    await page.locator('#stop').click();await page.waitForFunction(()=>state.session.status==='stopped');
    await page.locator('#detail-visibility button[data-detail-mode=all]').click();
    await page.evaluate(()=>{
      const run_id='compression-ui',emit=event=>dispatch({run_id,timestamp:Date.now()/1000,...event,sequence:state.sequence+1});
      const fields={id:'compression-fixture',step:2,generation:1,model:'grok-4.7',prefix_count:31,before:{messages:100,context_chars:100001,images:1}};
      emit({type:'loop_start'});emit({type:'context_compaction_start',...fields});
      emit({type:'context_compaction_transport',...fields,phase:'headers_received',http_status:200});
      emit({type:'context_compaction_end',...fields,after:{messages:15,context_chars:20001,images:1},elapsed:2,usage:{prompt_tokens:100,completion_tokens:10},summary:'## Continuation memory\n\n**Verified result**, pending work and exact paths.'});
      emit({type:'human_message',message_id:'summary-reply',source:'master',target:'user',images:[],content:'A user-facing reply.'});
    });
    const card=page.locator('.context-compaction');assert.equal(await card.count(),1);
    assert.match(await card.locator('summary').innerText(),/100,001 → 20,001 chars/);
    await card.locator('summary').click();assert.equal(await card.locator('h2').innerText(),'Continuation memory');
    assert.match(await card.locator('.compaction-meta').innerText(),/Input 100 · Output 10/);
    assert.match(await card.locator('.compaction-meta').innerText(),/1 images retained/);
    assert.equal(await page.evaluate(()=>[...modelRates.values()].filter(item=>item.purpose==='context_compaction').length),1);
    await page.locator('#detail-visibility button[data-detail-mode=human]').click();assert.equal(await card.isVisible(),false);
    await page.locator('.conversation-detail-control[data-detail-for="before/summary-reply"] button').click();assert.equal(await card.isVisible(),true);
    assert.deepEqual(errors,[]);result={passed:true,checks:['Root children remain visible; deeper non-Session folders can be displayed/hidden independently without hiding Session paths',
      'Per-directory filter choices survive refresh and exposed folders can create Sessions',
      'Runtime section is removed; workspace and Session archive remain',
      'Compression status updates one formatted card with before/after sizes, preserved images and usage; speed samples and detail filters include it'],external_model_api_calls:0,full_suite_run:false,errors};
  }finally{await cleanup({browser,server,temp});}
  report(result);
})().catch(error=>{console.error(error);process.exitCode=1;});
