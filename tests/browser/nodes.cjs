// One focused resource-panel/form flow; real SSH execution is in test_node_runtime.py.
const assert=require('node:assert/strict');
const {testDirectory,startServer,launchBrowser,cleanup,report}=require('./support.cjs');
(async()=>{
  const root=testDirectory('node-resources-ui-');let server,browser;const errors=[];
  try{
    server=startServer(root);const base=await server.ready;browser=await launchBrowser();const page=await browser.newPage({viewport:{width:1280,height:900}});
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base,{waitUntil:'domcontentloaded'});await page.waitForFunction(()=>state.config&&workspaceSync.loaded);
    await page.evaluate(async directory=>{const value=await api('/api/sessions',{action:'create',name:'Resource fixture',directory});install(value);},root);
    await page.waitForFunction(()=>state.session && !state.session.read_only);
    assert.equal(await page.locator('.session-resources .section-label').innerText(),'SESSION RESOURCES');
    assert.equal(await page.locator('#remote-node-list .remote-node').count(),0,'Local never appears as a selectable node');
    let submitted;
    await page.route('**/api/sessions/*/nodes',async route=>{
      submitted=route.request().postDataJSON();
      assert.equal(submitted.workspace,'/synthetic/project');assert.equal(submitted.ssh_target,'test-alias');assert.equal(submitted.execution_mode,'sandbox');
      await route.fulfill({json:{nodes:[{id:'local',status:'connected'},{id:submitted.node_id,name:submitted.name,workspace:submitted.workspace,status:'connected'}]}});
    });
    await page.locator('#add-node').click();await page.locator('#node-name').fill('Compute example');await page.locator('#node-target').fill('test-alias');await page.locator('#node-workspace').fill('/synthetic/project');
    await page.locator('#submit-node').click();await page.waitForFunction(()=>!document.querySelector('#node-dialog').open);
    assert.equal(await page.locator('#remote-node-list .remote-node').count(),1);
    assert.equal(await page.locator('.remote-node-name').innerText(),'Compute example');assert.equal(await page.locator('.remote-node-path').innerText(),'/synthetic/project');
    assert.equal(await page.evaluate(()=>state.session.nodes.filter(node=>node.id==='local').length),1);
    await page.evaluate(()=>{state.session.read_only=true;inspector();});assert.equal(await page.locator('#add-node').isDisabled(),true);
    assert.deepEqual(errors,[]);report({passed:true,checks:['compact resources','implicit local','node form payload','remote name/path','read-only restriction'],page_errors:errors});
  }finally{await cleanup({browser,server});}
})().catch(error=>{console.error(error);process.exitCode=1;});
