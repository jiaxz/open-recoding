// Saved Pool resources/logs are read remotely without starting workers.
const assert=require('node:assert/strict'),path=require('node:path');
const {execFileSync}=require('node:child_process');
const {testDirectory,startServer,launchBrowser,cleanup,report,project,environment}=require('./support.cjs');
(async()=>{
  const temp=testDirectory('pool-ui-');let server,browser,result;const errors=[];
  try {
    const seed=JSON.parse(execFileSync(path.join(project,'.venv/bin/python3'),['-B','-c',`
import json,sys,uuid
from pathlib import Path
from session import SessionStore
from multiagent.config import configuration
from node.packages import package_directory
from tools.python import PythonTools
root=Path(sys.argv[1]);store=SessionStore.create(root,'Pool browse fixture',{})
config=configuration({'network':False,'gpu':False,'tool_timeout':5,'package_directory':str(package_directory(root))})
pool=PythonTools(store,config,actor_info=lambda source:{'status':'running','permissions':{}},notify=lambda *args:uuid.uuid4().hex,observe=lambda *args:None)
try:
 process=pool.request('master','python_process',{'action':'create','name':'Fixture Python'})['process']['id']
 execution=pool.request('master','python',{'process_id':process,'code':'print("remote pool log")','mode':'sync'})['execution']['id']
finally:pool.close()
print(json.dumps({'archive':str(store.folder),'process_id':process,'execution_id':execution}))
`,temp],{cwd:project,env:environment(),encoding:'utf8'}));
    server=startServer(temp);const base=await server.ready;browser=await launchBrowser();
    const page=await browser.newPage({viewport:{width:1350,height:900}});page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base,{waitUntil:'domcontentloaded'});await page.waitForFunction(()=>state.config&&workspaceSync.loaded);
    await page.evaluate(async archive=>{const data=await api('/api/sessions',{path:archive,action:'browse'});install(data);},seed.archive);
    await page.waitForFunction(()=>state.session?.read_only);
    assert.equal(await page.locator('#python-pool').count(),0,'Pool resources are integrated into Jobs');
    assert.equal(await page.locator('#jobs-ready-count').innerText(),'0');
    assert.equal(await page.locator('.job-row').count(),0,'Closed processes and history are omitted');
    const output=await page.evaluate(async execution=>api(`/api/sessions/${state.session.id}/agents/master/executions/${execution}/output`),seed.execution_id);
    assert.equal(output.stdout,'remote pool log\n','Historical logs remain available through the server');
    await page.evaluate(({process_id})=>dispatch({type:'tool_service_updated',service:'python_pool',kind:'process',sequence:state.sequence+1,
      record:{...state.session.python_pool.processes[process_id],status:'ready'}},true),seed);
    assert.equal(await page.locator('#jobs-ready-count').innerText(),'1');
    assert.equal(await page.locator('#jobs-panel').getAttribute('open'),null);
    await page.locator('.jobs-heading').click();
    assert.match(await page.locator('.job-row').innerText(),/Fixture Python[\s\S]*ready/);
    await page.locator('.job-row>summary').click();
    assert.match(await page.locator('.job-row .job-id').innerText(),/Master/);
    assert.equal(await page.locator('.job-output').count(),0,'Ready does not reuse completed output');
    assert.deepEqual(errors,[]);result={passed:true,checks:['closed Pool history omitted without Resume','historical Python logs still available via HTTP','ready resource metadata and ownership in Jobs','live updates do not expand the panel'],external_model_api_calls:0,full_suite_run:false};
  }finally{await cleanup({browser,server,temp});}
  report(result);
})().catch(error=>{console.error(error);process.exitCode=1;});
