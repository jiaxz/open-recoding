// Opening a stopped archive is browsing; only an explicit Resume starts Master.
const assert=require('node:assert/strict'),path=require('node:path');
const {execFileSync}=require('node:child_process');
const {project,testDirectory,environment,startServer,launchBrowser,cleanup,report}=require('./support.cjs');

(async()=>{
  const temp=testDirectory('recoding-session-browse-'),root=path.join(temp,'workspace');
  const fixture=JSON.parse(execFileSync(path.join(project,'.venv/bin/python3'),['-c',[
    'import json,sys',
    'from session import SessionStore, SavedSession',
    'from multiagent.storage import MemberStore',
    'from multiagent.storage import atomic_json,read_json,instant',
    's=SessionStore.create(sys.argv[1],"browse-history",{})',
    's.update_metadata(pinned=True)',
    'SavedSession(s.folder).submit("先保留这条未读留言")',
    'sub="a"*32',
    'MemberStore.create_agent(s,sub,"stopped-helper",{})',
    'index=s.folder/"agents/index.json"',
    'team=read_json(index)',
    'team["agents"].append({"id":sub,"name":"stopped-helper","icon":"◇","status":"stopped","created_at":instant(),"attempt":0,"model":"grok-4.7","permissions":{}})',
    'atomic_json(index,team)',
    'p=SessionStore.create(sys.argv[1],"pinned-archive",{})',
    'p.update_metadata(pinned=True)',
    'print(json.dumps({"first":s.info(),"other":p.info(),"sub":sub}))',
  ].join('\n'),root],{cwd:project,env:environment(),encoding:'utf8'}));
  let server,browser,result;const errors=[],actions=[];
  try{
    server=startServer(root);const base=await server.ready;browser=await launchBrowser();
    const page=await browser.newPage({viewport:{width:1500,height:950}});page.on('pageerror',error=>errors.push(error.message));
    page.on('request',request=>{if(new URL(request.url()).pathname==='/api/sessions'&&request.method()==='POST')actions.push(request.postDataJSON().action);});
    await page.goto(base,{waitUntil:'domcontentloaded'});await page.waitForFunction(()=>workspaceSync.loaded&&!state.switching);
    const first=fixture.first,other=fixture.other;
    const own=id=>page.locator(`.directory-sessions > .session-row[data-id="${id}"] .session-item`);
    await own(first.id).click();await page.waitForFunction(id=>state.session?.id===id&&!state.switching,first.id);
    const notice=page.locator('#session-resume-notice');
    assert.equal(actions.at(-1),'browse');assert.equal(await notice.isVisible(),true);
    const snapshot=async id=>(await page.request.get(`${base}/api/sessions/${id}`)).json();
    let data=await snapshot(first.id);
    assert.equal(data.status,'stopped');assert.equal(data.agents[0].status,'stopped');
    assert.equal(data.events.filter(event=>event.type==='model_start').length,0);
    const input=await page.locator('#composer-body').boundingBox(),box=await notice.boundingBox();
    assert(box.y+box.height<=input.y&&input.y-box.y-box.height<12,'Resume notice belongs immediately above the input area');
    assert.equal(await page.locator('#stop').isDisabled(),true);
    await page.locator('#prompt').fill('浏览时留下的第二条消息');await page.locator('#send').click();
    data=await snapshot(first.id);assert.equal(data.status,'stopped');assert.equal(data.events.filter(event=>event.type==='model_start').length,0);
    await page.locator('#continue-task').click();
    await page.waitForFunction(()=>state.session?.status==='sleeping');
    data=await snapshot(first.id);assert.equal(data.agents[0].status,'stopped','User Resume only starts Master');
    assert.equal(await notice.isVisible(),false);const originalJob=data.run_id;
    await page.evaluate(()=>listSessions());
    const running=page.locator(`.running-sessions .session-row[data-id="${first.id}"]`);
    assert.equal(await running.isVisible(),true);assert.equal(await page.locator(`.pinned-sessions .session-row[data-id="${first.id}"]`).count(),0);
    assert(await page.locator('#session-list').evaluate(node=>node.firstElementChild.classList.contains('running-sessions')));
    assert.equal(await running.locator('.session-pin').getAttribute('aria-pressed'),'true','Automatic placement preserves manual pin metadata');
    await own(other.id).click();await page.waitForFunction(id=>state.session?.id===id&&!state.switching,other.id);
    assert.equal((await snapshot(other.id)).status,'stopped');assert.equal(await running.isVisible(),true);
    await running.locator('.session-item').click();await page.waitForFunction(id=>state.session?.id===id&&!state.switching,first.id);
    assert.equal((await snapshot(first.id)).run_id,originalJob,'Browsing a running Session must not restart its loop');
    await page.locator('#stop').click();await page.waitForFunction(()=>state.session?.status==='stopped');
    await page.evaluate(()=>listSessions());assert.equal(await running.count(),0);
    assert.equal(await notice.isVisible(),true);assert.equal(await page.locator(`.pinned-sessions .session-row[data-id="${first.id}"]`).isVisible(),true);
    const before=(await snapshot(first.id)).events.filter(event=>event.type==='model_start').length;
    await page.reload({waitUntil:'domcontentloaded'});await page.waitForFunction(()=>state.session?.status==='stopped'&&!state.switching);
    assert.equal(actions.at(-1),'browse');assert.equal(await notice.isVisible(),true);
    assert.equal((await snapshot(first.id)).events.filter(event=>event.type==='model_start').length,before,'Reloading never wakes Master');
    assert.deepEqual(errors,[]);
    result={passed:true,checks:['Opening and page reload browse stopped archives without Model calls; unread user mail remains queued',
      'Resume notice sits above the input; explicit Resume starts Master and leaves sub stopped',
      'Active Sessions precede manual pins, preserve pin metadata and remain running while browsing another archive',
      'Browsing a live Session keeps its run_id; Stop removes automatic placement and restores the browse reminder'],external_model_api_calls:0,full_suite_run:false,errors};
  }finally{await cleanup({browser,server});}
  report(result);
})().catch(error=>{console.error(error);process.exitCode=1;});
