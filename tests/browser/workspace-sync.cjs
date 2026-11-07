// Focused Session-manager synchronization test. No external model API calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {testDirectory, project, startServer, launchBrowser, cleanup, report: writeReport} = require('./support.cjs');

(async () => {
  const temp = testDirectory('agent-workspace-sync-');
  const root = path.join(temp, 'workspace');
  fs.mkdirSync(root);fs.writeFileSync(path.join(root,'initial.txt'),'initial');
  fs.mkdirSync(path.join(root,'before'));
  let server, browser, report;
  const errors = [], checks = [];
  try {
    server = startServer(root);
    const base = await server.ready;
    browser = await launchBrowser();
    const page=await browser.newPage({viewport:{width:1400,height:950}});
    page.on('pageerror',error=>errors.push(error.message));
    page.on('response',response=>{if(response.url().includes('/api/sessions')&&!response.ok())errors.push('HTTP '+response.status());});
    const directory=value=>page.locator('.workspace-group').filter({has:page.locator(':scope > summary > span[title="'+value+'"]')});
    const row=id=>page.locator(`.session-row[data-id="${id}"]`);
    const ready=()=>page.waitForFunction(()=>state.session?.status==='ready'&&!state.switching);
    await page.goto(base,{waitUntil:'networkidle'});
    assert.equal(await page.locator('.workspace-group').count(),1,'The selected root remains visible before its first Session');
    assert.equal(await page.locator('.workspace-file').count(),0);
    await page.locator('#new-session').click();await page.locator('#session-name').fill('Filesystem sync');
    await page.locator('#session-submit').click();await ready();
    const selected=await page.evaluate(()=>state.session.id);
    await page.locator('#prompt').fill('等待');await page.locator('#prompt').press('Enter');
    await page.waitForFunction(()=>document.querySelector('.tool-call[data-status=running]'));

    const generated=path.join(root,'generated');
    fs.mkdirSync(generated);fs.writeFileSync(path.join(generated,'output.txt'),'one');
    fs.renameSync(path.join(root,'before'),path.join(root,'renamed'));
    await page.waitForResponse(response=>new URL(response.url()).pathname==='/api/sessions'&&response.request().method()==='GET');
    assert.equal(await directory(generated).count(),0);
    assert.equal(await directory(path.join(root,'renamed')).count(),0);
    assert.equal(await page.locator('.workspace-file').count(),0);
    assert.equal(await page.locator('.workspace-group').count(),1);
    assert.equal(await page.evaluate(()=>state.session.status),'running');
    checks.push('The selected root stays visible; ordinary files and empty descendant directories stay absent while a real task runs');

    await directory(root).locator(':scope > summary > .workspace-add').click();
    assert.equal(await page.locator('#session-directory').inputValue(),root);
    await page.locator('#session-name').fill('Second session');await page.locator('#session-submit').click();await ready();
    const second=await page.evaluate(()=>state.session.id);
    assert.notEqual(second,selected);
    assert.equal(await directory(root).locator(':scope > .session-row').count(),2);
    async function removeSession(id) {
      await row(id).locator('.session-more').click();await page.locator('#menu-delete-session').click();
      await page.locator('#confirm-delete').click();await row(id).waitFor({state:'detached'});
    }
    await removeSession(second);
    await row(selected).locator('.session-item').click();
    await page.waitForFunction(()=>state.session?.status==='running'&&!state.switching);
    assert.equal(fs.readFileSync(path.join(root,'initial.txt'),'utf8'),'initial');
    checks.push('The directory + button creates in the existing Session workspace; deleting that Session preserves its workspace and the original running task');

    await directory(root).locator(':scope > summary').click();
    assert.equal(await page.evaluate(()=>state.session.id),selected);

    const imported=spawnSync('python3',['-c',"import json,sys;from session.store import SessionStore;print(json.dumps(SessionStore.create(sys.argv[1],'Imported archive',{}).info()))",path.join(root,'projects','imported')],{cwd:project,encoding:'utf8'});
    assert.equal(imported.status,0,imported.stderr);const saved=JSON.parse(imported.stdout);
    await row(saved.id).waitFor({state:'attached'});
    assert.equal(await directory(root).getAttribute('open'),null);
    assert.equal(await directory(path.join(root,'projects')).count(),0);
    assert.match(await directory(saved.directory).locator(':scope > summary').textContent(),/projects\/imported/);
    const movedWorkspace=path.join(root,'projects','moved'),moved=path.join(movedWorkspace,'.recoding','moved');
    fs.mkdirSync(path.dirname(moved),{recursive:true});fs.renameSync(saved.session_directory,moved);
    await page.waitForFunction(({id,workspace})=>document.querySelector(`.session-row[data-id="${id}"]`)?.closest('.workspace-group').dataset.directory===workspace,{id:saved.id,workspace:movedWorkspace});
    await directory(saved.directory).waitFor({state:'detached'});
    fs.rmSync(moved,{recursive:true});await row(saved.id).waitFor({state:'detached'});
    await directory(movedWorkspace).waitFor({state:'detached'});
    assert.equal(await page.evaluate(()=>state.session.id),selected);
    assert.equal(await page.evaluate(()=>state.session.status),'running');
    await page.locator('#stop').click();await ready();
    await directory(root).locator(':scope > summary').click();
    await removeSession(selected);
    assert.equal(await page.locator('.workspace-group').count(),1,'The root remains available for creating another Session');
    assert.equal(fs.readFileSync(path.join(root,'initial.txt'),'utf8'),'initial');
    checks.push('Server-side Session creation/move/deletion updates automatically, preserves collapse/selection and merges path-only ancestors while retaining the root');
    assert.deepEqual(errors,[]);
    report={passed:true,checks,errors,external_model_api_calls:0,full_suite_run:false};
  } finally {
    await cleanup({browser, server, temp});
  }
  writeReport(report);
})().catch(error=>{console.error(error);process.exitCode=1;});
