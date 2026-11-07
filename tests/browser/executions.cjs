// Focused Job sidebar, scoped remote log transport and bounded incremental reads.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {execFileSync}=require('node:child_process');
const {testDirectory,startServer,launchBrowser,cleanup,report,project,environment}=require('./support.cjs');
(async()=>{
  const temp=testDirectory('jobs-ui-');let server,browser,result;const errors=[];
  try{
    const seed=JSON.parse(execFileSync(path.join(project,'.venv/bin/python3'),['-B','-c',`
import json,sys,time
from pathlib import Path
from session import Session
root=Path(sys.argv[1]);session=Session.create(root,'Bash log fixture',config={'demo':True,'network':False,'gpu':False,'tool_timeout':5})
try:
 end=time.monotonic()+10
 while session.master.status!='sleeping':
  assert session.master.status!='failed',session.master.error
  assert time.monotonic()<end
  time.sleep(.02)
 result=session.team.dispatch('master','bash',{'command':"printf 'hello <script>window.untrustedJob=true</script>\\n'; printf warning >&2",'name':'fixture build'})
 assert result['exit_code']==0,result
 print(json.dumps({'archive':str(session.store.folder),'owner':session.id,'execution':result['execution']}))
finally:session.close()
`,temp],{cwd:project,env:environment(),encoding:'utf8'}));
    server=startServer(temp);const base=await server.ready;browser=await launchBrowser();
    const page=await browser.newPage({viewport:{width:1300,height:900}});page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base,{waitUntil:'domcontentloaded'});await page.waitForFunction(()=>state.config&&workspaceSync.loaded);
    assert.equal(await page.locator('#background-jobs').isVisible(),false);
    await page.evaluate(async archive=>{const data=await api('/api/sessions',{path:archive,action:'browse'});install(data);},seed.archive);
    await page.waitForFunction(()=>state.session?.read_only);
    const folder=path.join(temp,'.recoding','runtime','executions',seed.owner,seed.execution.id);
    assert.equal(await page.locator('#jobs-panel').getAttribute('open'),null,'Jobs starts collapsed');
    assert.equal(await page.locator('#jobs-run-count').innerText(),'0');
    assert.equal(await page.locator('.job-row').count(),0,'Finished executions are omitted');
    const task={...seed.execution,status:'running',mode:'sync',created_at:Date.now()/1000-4000,started_at:Date.now()/1000-3,finished_at:null},ready={id:'f'.repeat(32),name:'Stateful Python',status:'ready',lifetime:'persistent',owner:'master',creator:'master',created_at:Date.now()/1000};
    await page.evaluate(({task,ready})=>{
      const emit=(service,kind,record)=>dispatch({type:'tool_service_updated',service,kind,record,sequence:state.sequence+1});
      emit('bash_pool','execution',task);emit('python_pool','process',ready);
    },{task,ready});
    assert.equal(await page.locator('#jobs-run-count').innerText(),'1');
    assert.equal(await page.locator('#jobs-ready-count').innerText(),'1');
    assert.equal(await page.locator('#jobs-panel').getAttribute('open'),null,'Updates never force the panel open');
    await page.locator('.jobs-heading').click();
    assert.equal(await page.locator('.job-row').count(),2);
    const running=page.locator('.job-row[data-status=run]');
    assert.equal(await running.locator('.job-call-mode').innerText(),'sync');
    assert.equal(await running.locator('.job-lifetime').innerText(),'无状态');
    const firstElapsed=parseFloat(await running.locator('.job-call-elapsed').innerText());
    assert(firstElapsed>=3&&firstElapsed<15,'Timer starts at this execution, not process creation or queue time');
    await page.waitForFunction(previous=>parseFloat(document.querySelector('.job-row[data-status=run] .job-call-elapsed').textContent)>previous,firstElapsed);
    const idle=page.locator('.job-row[data-status=ready]');
    assert.equal(await idle.locator('.job-call-mode').innerText(),'—');
    assert.equal(await idle.locator('.job-lifetime').innerText(),'有状态');
    assert.equal(await idle.locator('.job-call-elapsed').innerText(),'空闲');
    await page.evaluate(ready=>{
      const call={id:'d'.repeat(32),process_id:ready.id,kind:'python',owner:'master',source:'master',mode:'async',
        lifetime:'persistent',status:'queued',created_at:Date.now()/1000-900};
      window.queuedFixture=call;
      dispatch({type:'tool_service_updated',service:'python_pool',kind:'execution',record:call,sequence:state.sequence+1});
    },ready);
    const persistent=page.locator(`.job-row[data-process-id="${ready.id}"]`);
    assert.equal(await persistent.locator('.job-call-mode').innerText(),'async');
    assert.equal(await persistent.locator('.job-call-elapsed').innerText(),'排队中');
    await page.evaluate(()=>dispatch({type:'tool_service_updated',service:'python_pool',kind:'execution',
      record:{...queuedFixture,status:'running',started_at:Date.now()/1000},sequence:state.sequence+1}));
    assert(parseFloat(await persistent.locator('.job-call-elapsed').innerText())<3,'A fresh call resets its execution clock');
    await page.evaluate(()=>dispatch({type:'tool_service_updated',service:'python_pool',kind:'execution',
      record:{...queuedFixture,status:'succeeded',finished_at:Date.now()/1000},sequence:state.sequence+1}));
    assert.equal(await persistent.locator('.job-call-mode').innerText(),'—');
    assert.equal(await persistent.locator('.job-call-elapsed').innerText(),'空闲');

    await page.locator('.job-row[data-status=run]>summary').click();
    await page.waitForFunction(()=>document.querySelector('.job-output').textContent.includes('hello'));
    assert.match(await page.locator('.job-output').innerText(),/hello <script>/);
    assert.equal(await page.evaluate(()=>window.untrustedJob),undefined);
    await page.evaluate(()=>inspector());
    assert.equal(await page.locator('.job-row[data-status=run]').getAttribute('open'),'','Live refresh retains expanded output');
    fs.appendFileSync(path.join(folder,'stdout.log'),'next line\n');
    await page.locator('.job-list button').click();
    await page.waitForFunction(()=>document.querySelector('.job-output').textContent.includes('next line'));
    const output=await page.locator('.job-output').innerText();assert.equal(output.split('hello').length-1,1);
    assert.match(output,/warning/);
    const missing=await page.evaluate(async()=>{return api(`/api/sessions/${state.session.id}/agents/${'f'.repeat(32)}/executions`).then(()=>200,error=>error.status);});
    assert.equal(missing,404);
    // Ready processes have no active execution and must not display old output.
    await page.locator('.job-row[data-status=ready]>summary').click();
    assert.equal(await page.locator('.job-row[data-status=ready] .job-output').count(),0);
    await page.evaluate(ready=>{
      for(let i=0;i<8;i++)dispatch({type:'tool_service_updated',service:'python_pool',kind:'process',sequence:state.sequence+1,
        record:{...ready,id:i.toString(16).padStart(32,'0'),name:`Ready ${i}`}});
    },ready);
    const list=page.locator('#job-list');
    assert.equal(await list.evaluate(node=>node.scrollHeight>node.clientHeight),true);
    const before=await list.evaluate(node=>node.clientHeight),handle=await page.locator('#job-list-resizer').boundingBox();
    await page.mouse.move(handle.x+handle.width/2,handle.y+handle.height/2);await page.mouse.down();
    await page.mouse.move(handle.x+handle.width/2,handle.y+handle.height/2+70);await page.mouse.up();
    assert((await list.evaluate(node=>node.clientHeight))>before+50,'Jobs height can be dragged');
    await page.evaluate(task=>dispatch({type:'tool_service_updated',service:'bash_pool',kind:'execution',sequence:state.sequence+1,
      record:{...task,status:'succeeded',finished_at:Date.now()/1000}}),task);
    assert.equal(await page.locator('#jobs-run-count').innerText(),'0');
    assert.equal(await page.locator('.job-row[data-status=run]').count(),0,'Finished temporary resource disappears');
    assert.equal(await page.locator('#jobs-ready-count').innerText(),'9');
    await page.locator('.jobs-heading').click();assert.equal(await list.isVisible(),false);
    await page.locator('.jobs-heading').click();
    assert((await list.evaluate(node=>node.clientHeight))>before+50,'Collapsed panel retains height');

    // Agent metrics update through the Session metadata event, with stable creation ordering.
    await page.evaluate(()=>{
      const now=Date.now()/1000;
      dispatch({type:'agents_updated',sequence:state.sequence+1,team:state.session.team,agents:[
        {id:'a'.repeat(32),name:'Older',status:'sleeping',created_at:now-200},
        {id:'b'.repeat(32),name:'Newer',status:'sleeping',created_at:now-100}],agent_token_usage:{
          master:{input_tokens:123456,compaction_saved_tokens:560000},
          ['a'.repeat(32)]:{input_tokens:24000,compaction_saved_tokens:60000},
          ['b'.repeat(32)]:{input_tokens:35000,compaction_saved_tokens:0}}});
    });
    assert.deepEqual(await page.locator('#agent-list .agent-list-item').evaluateAll(nodes=>nodes.map(node=>node.dataset.agentId)),['master','b'.repeat(32),'a'.repeat(32)]);
    assert.equal(await page.locator('#agent-list time,.agent-created').count(),0);
    const master=page.locator('#agent-list [data-agent-id=master]');
    assert.equal(await master.locator('[data-metric=input]').innerText(),'Input 123.5k');
    assert.equal(await master.locator('[data-metric=saved]').innerText(),'已压缩 ≈560.0k');
    assert.match(await master.locator('[data-metric=input]').getAttribute('title'),/123,456/);
    await page.evaluate(()=>dispatch({type:'model_end',run_id:'numeric',step:1,usage:{prompt_tokens:130000},
      context:{last_input_tokens:130000,compaction_saved_tokens:570000},sequence:state.sequence+1}));
    // Production agents_updated is authoritative; an ordinary update should preserve the selected row and order.
    await page.evaluate(()=>dispatch({type:'agents_updated',agents:state.session.agents,team:state.session.team,
      agent_token_usage:{...state.session.agent_token_usage,master:{input_tokens:130000,compaction_saved_tokens:570000}},sequence:state.sequence+1}));
    assert.equal(await master.locator('[data-metric=input]').innerText(),'Input 130.0k');
    assert.equal(await master.locator('[data-metric=saved]').innerText(),'已压缩 ≈570.0k');
    assert(await master.evaluate(node=>node.getBoundingClientRect().height<=38),'Agent rows remain compact');
    assert(await master.locator('.agent-token-usage').evaluate(node=>node.scrollWidth<=node.clientWidth),'Token metrics fit without horizontal overflow');
    assert(await page.locator('.job-call-meta').first().evaluate(node=>node.scrollWidth<=node.clientWidth),'Process metadata stays within the sidebar');


    // Python/Bash call cards show explicit/default waiting mode, even when code was truncated.
    await page.evaluate(()=>selectAgent('master'));
    await page.locator('#detail-visibility button[data-detail-mode=all]').click();
    await page.evaluate(()=>{
      const run_id='execution-mode',emit=event=>dispatch({run_id,timestamp:Date.now()/1000,...event,sequence:state.sequence+1});
      emit({type:'loop_start'});
      for(const [index,[name,mode]] of [['python','sync'],['python','async'],['bash','sync'],['bash','async']].entries()) {
        const fields={step:101,number:index+1,call_id:`mode-${index}`,name};
        const args={[name==='python'?'code':'command']:name==='python'?'print(1)':'printf 1',...(index!==0?{mode}:{})};
        emit({type:'tool_start',...fields,arguments:JSON.stringify(args)});
        emit({type:'tool_end',...fields,elapsed:.01,result:{exit_code:0,stdout:'1'}});
      }
      emit({type:'tool_start',step:101,number:5,call_id:'long-mode',name:'python',arguments:'{"code":"truncated',
        source_preview:'print(1)',source_truncated:true,source_length:20000,source_language:'python',execution_mode:'async'});
    });
    assert.deepEqual(await page.locator('.agent-run[data-run-id="execution-mode"] .tool-mode').allTextContents(),['sync','async','sync','async','async']);
    assert.equal(await page.evaluate(()=>state.session.read_only),true,'Rendering never resumes the archive');
    assert.deepEqual(errors,[]);result={passed:true,checks:['compact live Process list with run/ready counts','current-call sync/async, stateful/stateless and execution-only live timers; idle/queued semantics','latest Agent Input and estimated compaction savings, no creation-age text, unchanged newest-first ordering','compact scrollable panel with drag height preserved','Python/Bash sync/async badges including truncated source','stable incremental server logs remain inert; unknown Agent rejected'],external_model_api_calls:0,full_suite_run:false};
  }finally{await cleanup({browser,server,temp});}
  report(result);
})().catch(error=>{console.error(error);process.exitCode=1;});
