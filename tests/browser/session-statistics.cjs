// Focused sidebar regression. Offline Demo, temporary server/workspace only.
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {execFileSync}=require('node:child_process');
const {testDirectory, startServer,launchBrowser,cleanup,report,project,environment}=require('./support.cjs');
(async()=>{
  const temp=testDirectory('recoding-statistics-');
  let server,browser,result;const errors=[];
  try {
    const parent=path.join(temp,'projects'),leaf=path.join(parent,'middle','work'),ordinary=path.join(temp,'ordinary');
    fs.mkdirSync(ordinary);
    execFileSync('python3',['-c','import sys; from session.store import SessionStore; SessionStore.create(sys.argv[1],"discovered",{})',leaf],{cwd:project,env:environment()});
    server=startServer(temp);const base=await server.ready;
    browser=await launchBrowser();const page=await browser.newPage({viewport:{width:1300,height:900}});
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base,{waitUntil:'domcontentloaded'});
    await page.waitForFunction(()=>state.config&&workspaceSync.loaded);
    const directory=value=>page.locator(`.workspace-group[data-directory="${value}"]`);
    await directory(leaf).locator(':scope > summary').click();await directory(parent).locator(':scope > summary').click();
    await page.evaluate(()=>listSessions());
    assert.equal(await directory(parent).getAttribute('open'),null);
    assert.equal(await directory(leaf).getAttribute('open'),null);
    await page.locator('#discover-sessions').click();await page.waitForFunction(()=>!state.discoveringSessions&&!listingRequest);
    for(const value of [parent,path.join(parent,'middle'),leaf])assert.equal(await directory(value).getAttribute('open'),'','Discover All overrides deliberate Session-path collapses');
    assert.equal(await directory(ordinary).getAttribute('open'),null,'Discover All does not expand unrelated folders');
    await page.locator('#new-session').click();await page.locator('#session-submit').click();
    await page.waitForFunction(()=>state.session?.status==='sleeping'&&!state.switching);
    await page.locator('#stop').click();await page.waitForFunction(()=>state.session.status==='stopped');
    await page.evaluate(()=>{
      // Keep the renderer's wall clock deterministic while its real one-second
      // timer runs. No model events are dispatched during idle advancement.
      window.speedNow=Math.floor(Date.now()/1000);window.speedOrigin=speedNow;
      Date.now=()=>speedNow*1000;
      state.session.created_at=new Date((speedOrigin-10800)*1000).toISOString();
      state.session.team={...state.session.team,prompt_tokens:1234567,completion_tokens:7654};
      const samples=[[-1800,100,'gemini-4-argon'],[-900,200,'grok-4.7'],[0,150,'gemini-3.8-flash'],[0,150,'gemini-3.8-flash']];
      modelRates.clear();
      for(const [index,[offset,tokens,model]] of samples.entries())modelRates.set(`fixture/${index}`,{
        run_id:'fixture',step:index+1,timestamp:speedOrigin+offset,elapsed:10,output_tokens:tokens,rate:tokens/10,
        model,agent_id:index===3 ? 'peer' : 'master',agent_name:index===3 ? 'Peer' : 'Master'});
      inspector();renderSessionSpeed();
    });
    assert.equal(await page.locator('#team-summary details').getAttribute('open'),null);
    assert.equal(await page.locator('.team-token-total[data-metric=prompt_tokens] strong').innerText(),'1,234,567');
    assert.equal(await page.locator('.team-token-total[data-metric=completion_tokens] strong').innerText(),'7,654');
    await page.locator('#team-summary summary').click();assert.equal(await page.locator('.team-stat-grid').isVisible(),true);
    await page.locator('#team-summary summary').click();
    const chart=page.locator('#session-speed .speed-chart');
    const preset=value=>page.locator(`.speed-ranges button[data-speed-range="${value}"]`);
    const readWindow=()=>page.locator('#session-speed').evaluate(node=>({start:Number(node.dataset.viewStart),end:Number(node.dataset.viewEnd),preset:node.dataset.rangePreset}));
    assert.equal(await page.locator('.speed-pan,input[type=range][aria-label="平移速度图的时间窗口"]').count(),0);
    assert.equal(await preset('1h').getAttribute('aria-pressed'),'true');
    let window=await readWindow();assert.equal(window.end-window.start,3600);assert.equal(window.preset,'1h');
    await preset('10m').click();window=await readWindow();assert.equal(window.end-window.start,600);
    await preset('1d').click();window=await readWindow();assert.equal(window.end-window.start,10800);
    await preset('all').click();assert.equal(await page.locator('#session-speed').getAttribute('data-full-range'),'true');
    await preset('1h').click();
    assert.equal(await page.locator('#session-speed svg').count(),1);
    assert.equal(await chart.locator('.speed-session-line').count(),1);
    assert.equal(await chart.locator('.speed-line').count(),3);
    assert.equal(await chart.getAttribute('data-latest-rate'),'30.000000','Parallel Agent rates must add, never average');
    assert.equal(await page.locator('.speed-total-value strong').innerText(),'30.0');
    assert.equal(await chart.locator('.speed-now-label').textContent(),'NOW');
    assert.equal(await page.evaluate(()=>{
      const svg=document.querySelector('.speed-chart');
      const curves=[...svg.querySelectorAll('.speed-line,.speed-session-line')];
      return new Set(curves.map(curve=>curve.parentElement)).size;
    }),1,'Both series must share one clipped coordinate system');
    const initial=await page.locator('#session-speed').getAttribute('data-domain-end');
    await page.evaluate(()=>speedNow+=5);
    await page.waitForFunction(()=>Number(document.querySelector('#session-speed').dataset.domainEnd)===speedNow);
    window=await readWindow();assert.equal(window.end-window.start,3600);assert.equal(window.end,await page.evaluate(()=>speedNow));
    assert.equal(Number(await page.locator('#session-speed').getAttribute('data-domain-end'))-Number(initial),5);
    assert.equal(await chart.getAttribute('data-latest-rate'),'15.000000');
    await page.evaluate(()=>speedNow+=6);
    await page.waitForFunction(()=>document.querySelector('.speed-chart').dataset.latestRate==='0.000000');
    assert.equal(await page.locator('#session-speed').getAttribute('data-samples'),'4');
    assert.equal(await chart.locator('.speed-point').count(),4,'Idle advancement must not invent Model call samples');

    const originalCall=await chart.locator('.speed-line').first().getAttribute('d');
    const originalTotal=await chart.locator('.speed-session-line').getAttribute('d');
    await chart.focus();await page.keyboard.press('+');
    assert.equal(await page.locator('#session-speed').getAttribute('data-full-range'),'false');
    assert.notEqual(await chart.locator('.speed-line').first().getAttribute('d'),originalCall);
    assert.notEqual(await chart.locator('.speed-session-line').getAttribute('d'),originalTotal);
    assert.equal(await page.locator('.speed-ranges').isVisible(),false);
    await page.keyboard.press('ArrowLeft');
    await chart.focus();
    const manual=await page.evaluate(()=>{
      const data=document.querySelector('#session-speed').dataset;return {start:data.viewStart,end:data.viewEnd};
    });
    await page.evaluate(()=>speedNow+=12);
    await page.waitForFunction(()=>Number(document.querySelector('#session-speed').dataset.domainEnd)===speedNow);
    assert.deepEqual(await page.evaluate(()=>{
      const data=document.querySelector('#session-speed').dataset;return {start:data.viewStart,end:data.viewEnd};
    }),manual,'Clock ticks must preserve a manually selected historical window');
    assert.equal(await chart.evaluate(svg=>document.activeElement===svg),true,'Clock ticks preserve keyboard focus');
    await page.evaluate(()=>renderSessionSpeed());
    assert.deepEqual(await readWindow(),{...manual,start:Number(manual.start),end:Number(manual.end),preset:'custom'},'New renderer updates also preserve the custom window');
    assert.equal(await page.locator('.speed-ranges').isVisible(),false);
    await page.locator('.speed-restore-range').click();window=await readWindow();assert.equal(window.preset,'1h');assert.equal(window.end-window.start,3600);
    assert.equal(window.end,await page.evaluate(()=>speedNow));assert.equal(await page.locator('.speed-ranges').isVisible(),true);
    await chart.click({position:{x:100,y:50}});assert.equal(await page.locator('.speed-ranges').isVisible(),true,'A simple click keeps automatic tracking');
    const box=await chart.boundingBox(),beforeDrag=await readWindow(),dragX=box.x+box.width*.6,dragY=box.y+box.height*.5;
    await page.mouse.move(dragX,dragY);await page.mouse.down();await page.mouse.move(dragX+20,dragY);
    assert.equal(await page.locator('.speed-ranges').isVisible(),false);
    assert((await readWindow()).start<beforeDrag.start,'Dragging pans toward earlier data');
    await page.mouse.move(dragX,dragY);await page.mouse.up();
    assert(Math.abs((await readWindow()).start-beforeDrag.start)<1,'Dragging back to the origin restores its coordinates without leaving manual mode');
    await page.keyboard.press('Home');
    assert.equal(await preset('all').getAttribute('aria-pressed'),'true');assert.equal(await page.locator('.speed-ranges').isVisible(),true);
    assert.equal(await page.locator('#session-speed').getAttribute('data-full-range'),'true');
    assert.equal(Number(await page.locator('#session-speed').getAttribute('data-view-end')),await page.evaluate(()=>speedNow));

    await page.evaluate(()=>{modelRates.clear();renderSessionSpeed();speedNow+=2;});
    await page.waitForFunction(()=>Number(document.querySelector('#session-speed').dataset.domainEnd)===speedNow);
    assert.equal(await page.locator('#session-speed').getAttribute('data-samples'),'0');
    assert.equal(await page.locator('.speed-total-value strong').innerText(),'—');
    assert.equal(await chart.locator('.speed-session-line').count(),0);
    assert.equal(await chart.locator('.speed-now').count(),1);
    assert.deepEqual(errors,[]);
    result={passed:true,checks:['Collapsed All Agents emphasizes cumulative Input/Output tokens; expanded details remain available',
      'Discover All rescans and reveals every Session path despite manual collapse, leaving unrelated branches closed',
      'Four rolling time presets replace the slider, default to one hour and follow current time',
      'One chart shares time/tok/s axes, model colors and zoom/pan for both series',
      'One-second clock advances without model events; summed throughput decays to zero without invented samples',
      'Manual zoom/pan hides presets, freezes the window through ticks/rerenders, and can restore automatic tracking',
      'An empty history still advances its timeline and does not invent throughput'],external_model_api_calls:0,errors,full_suite_run:false};
  } finally {await cleanup({browser,server,temp});}
  report(result);
})().catch(error=>{console.error(error);process.exitCode=1;});
