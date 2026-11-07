// A stale browser selection must not turn successful directory discovery into failure.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {execFileSync}=require('node:child_process');
const {project,testDirectory,environment,startServer,launchBrowser,cleanup,report}=require('./support.cjs');

(async()=>{
  const temp=testDirectory('recoding-stale-startup-'),root=path.join(temp,'workspace');
  const fixture=JSON.parse(execFileSync(path.join(project,'.venv/bin/python3'),['-B','-c',[
    'import json,sys',
    'from pathlib import Path',
    'from session import SessionStore',
    'r=Path(sys.argv[1]);valid=SessionStore.create(r/"workspace"/"current","Current",{})',
    'outside=SessionStore.create(r/"outside","Outside",{})',
    'old=SessionStore.create(r/"workspace"/"old-format","Old format",{})',
    'p=old.folder/"session.json";data=json.loads(p.read_text());data["version"]=4;p.write_text(json.dumps(data))',
    'print(json.dumps({"valid":valid.navigation_info(),"outside":outside.navigation_info(),"old":old.navigation_info()}))',
  ].join(';'),temp],{cwd:project,env:environment(),encoding:'utf8'}));
  const missing=path.join(temp,'previous-project','.recoding','missing-session');
  const cached={id:'a'.repeat(32),path:missing.replace('/.recoding/','/.recoding.../')};
  let server,browser;const errors=[],checks=[],opened=[];
  try{
    server=startServer(root);const base=await server.ready;browser=await launchBrowser();
    const context=await browser.newContext();
    await context.addInitScript(value=>{
      if(!sessionStorage.getItem('test-initialized')){
        localStorage.setItem('recoding-session',JSON.stringify(value));
        sessionStorage.setItem('recoding-session',JSON.stringify(value));
        sessionStorage.setItem('test-initialized','yes');
      }
    },cached);
    const page=await context.newPage();page.setDefaultTimeout(10000);
    page.on('pageerror',error=>errors.push(error.message));
    page.on('request',request=>{
      if(request.method()==='POST'&&new URL(request.url()).pathname==='/api/sessions')opened.push(request.postDataJSON());
    });
    const ready=()=>page.waitForFunction(()=>workspaceSync.operation?.status==='done'&&!state.switching);
    const save=async value=>page.evaluate(value=>{
      localStorage.setItem('recoding-session',JSON.stringify(value));sessionStorage.setItem('recoding-session',JSON.stringify(value));
    },value);
    const saved=()=>page.evaluate(()=>({local:localStorage.getItem('recoding-session'),tab:sessionStorage.getItem('recoding-session')}));
    await page.goto(base,{waitUntil:'domcontentloaded'});await ready();
    assert.equal(await page.evaluate(()=>state.session),null);
    assert.equal(await page.locator(`.session-row[data-id="${fixture.valid.id}"]`).count(),1);
    assert.equal(await page.locator('#workspace-sync').getAttribute('data-state'),'done');
    assert.match(await page.locator('#server-warning').innerText(),/已移动、删除或格式不兼容/);
    assert.equal(await page.locator('#toast').isVisible(),false);
    assert.deepEqual(await saved(),{local:null,tab:null});
    assert.equal(fs.existsSync(path.dirname(missing)),false,'A stale preference never recreates an old server directory');
    const absent=await page.evaluate(async missing=>{
      try{await api('/api/sessions',{action:'browse',path:missing});return {};}
      catch(error){return {status:error.status,code:error.code,message:error.message};}
    },missing);
    assert.equal(absent.status,404);assert.equal(absent.code,'session_not_found');
    assert.doesNotMatch(absent.message,/Errno/);
    checks.push('Deleted/relocated container: current directory still synchronizes, stale selection clears, archive remains missing, direct API returns meaningful 404');

    await save({id:fixture.valid.id,path:cached.path});await page.reload({waitUntil:'domcontentloaded'});await ready();
    assert.equal(await page.evaluate(()=>state.session?.id),fixture.valid.id);
    assert.equal(await page.evaluate(()=>state.session?.read_only),true);
    assert.equal(opened.at(-1).id,fixture.valid.id);assert.equal(opened.at(-1).path,undefined);
    assert.equal(JSON.parse((await saved()).local).path,fixture.valid.session_directory);
    checks.push('A moved archive found by persistent ID opens at its current server path without Resume');

    await save({id:fixture.outside.id,path:fixture.outside.session_directory});
    await page.reload({waitUntil:'domcontentloaded'});await ready();
    assert.equal(await page.evaluate(()=>state.session?.id),fixture.outside.id);
    assert.equal(await page.evaluate(()=>state.session?.read_only),true);
    checks.push('A still-valid explicitly selected archive outside the scan root continues to restore read-only');

    await save({id:fixture.old.id,path:fixture.old.session_directory});
    await page.reload({waitUntil:'domcontentloaded'});await ready();
    assert.equal(await page.evaluate(()=>state.session),null);assert.deepEqual(await saved(),{local:null,tab:null});
    assert.equal(JSON.parse(fs.readFileSync(path.join(fixture.old.session_directory,'session.json'))).version,4);
    checks.push('Unsupported old format: browser preference clears while server archive remains untouched');

    await save(cached);
    await page.route('**/api/sessions',async route=>{
      if(route.request().method()==='POST')await route.fulfill({status:403,json:{error:'Synthetic archive permission failure'}});
      else await route.continue();
    });
    await page.reload({waitUntil:'domcontentloaded'});
    await page.waitForFunction(()=>workspaceSync.operation?.status==='error'&&!state.switching);
    assert.match(await page.locator('#workspace-sync-label').innerText(),/Session 恢复失败/);
    assert.match(await page.locator('#workspace-sync-detail').innerText(),/permission failure/);
    assert((await saved()).local,'Permission/connection failures must not silently erase the selection');
    await page.unroute('**/api/sessions');
    checks.push('Permission failure stays visible as archive restoration failure, preserving the saved selection');

    await page.route('**/api/sessions?*',route=>route.fulfill({status:500,json:{error:'Synthetic directory scan failure'}}));
    await page.reload({waitUntil:'domcontentloaded'});
    await page.waitForFunction(()=>workspaceSync.operation?.status==='error');
    assert.match(await page.locator('#workspace-sync-label').innerText(),/当前工作目录同步失败/);
    assert.match(await page.locator('#workspace-sync-detail').innerText(),/directory scan failure/);
    for(const item of Object.values(fixture)){
      assert.equal(fs.existsSync(path.join(item.session_directory,'state.json')),false,'Browsing never starts an Agent');
      assert.equal(fs.readdirSync(path.join(item.session_directory,'runs')).length,0);
    }
    assert.deepEqual(errors,[]);
    checks.push('Actual directory synchronization errors remain visible; every archive stays without worker state or model calls');
    report({passed:true,checks,page_errors:errors,external_model_api_calls:0,full_suite_run:false});
  }finally{await cleanup({browser,server});}
})().catch(error=>{console.error(error);process.exitCode=1;});
