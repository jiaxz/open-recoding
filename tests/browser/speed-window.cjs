// The visible curves determine Y scale; no model, real Session or server needed.
const assert=require('node:assert/strict'),path=require('node:path');
const {launchBrowser,project,report}=require('./support.cjs');
(async()=>{
  const browser=await launchBrowser();const errors=[];
  try {
    const page=await browser.newPage();page.on('pageerror',error=>errors.push(error.message));
    await page.setContent('<div id="chart"></div>');
    await page.addScriptTag({path:path.join(project,'apps/web/recoding_web/static/statistics.js')});
    await page.evaluate(()=>{
      const now=Date.now()/1000;window.origin=now;window.chart=document.querySelector('#chart');
      window.sample=(offset,rate,elapsed=10,agent='master')=>({timestamp:origin+offset,rate,elapsed,
        output_tokens:rate*elapsed,model:'grok-4.7',step:1,agent_id:agent});
      window.draw=samples=>AgentStatistics.renderSpeed(chart,samples,undefined,{startedAt:origin-7200});
      draw([sample(-7100,10000),sample(-7000,15),sample(-1800,20),sample(-900,30),sample(-100,40)]);
    });
    const range=()=>page.locator('#chart').evaluate(el=>({min:+el.dataset.yMin,max:+el.dataset.yMax}));
    assert.ok((await range()).max<50,'An old offscreen spike cannot flatten the recent hour');
    const preset=value=>page.locator(`[data-speed-range="${value}"]`);
    await preset('all').click();assert.ok((await range()).max>10000);
    await preset('1h').click();
    const svg=page.locator('svg');await svg.focus();await page.keyboard.press('+');
    assert.ok((await range()).max<50,'Keyboard zoom recalculates Y');
    for(let n=0;n<16;n++)await page.keyboard.press('ArrowLeft');
    assert.ok((await range()).max>1000,'Panning to the old spike expands Y');
    await preset('10m').evaluate(button=>button.click());
    assert.ok((await range()).max<50,'Preset selection resets the visible Y scale');
    await page.evaluate(()=>draw([sample(-1000,10000,1),sample(-900,10,1)]));
    assert.ok((await range()).max<=1,'Lines ending before the window are not extrapolated');
    await page.evaluate(()=>draw([sample(-1000,100,1),sample(0,10,1)]));
    const crossing=await range();
    assert.ok(crossing.max>64&&crossing.max<70,'Use the interpolated 64 tok/s edge, not the 100 tok/s offscreen endpoint');
    await page.evaluate(()=>draw([sample(-5,50),sample(-5,50,10,'peer')]));
    assert.ok((await range()).max>=100,'Concurrent summed-curve peaks fit the same Y axis');
    await page.evaluate(()=>draw([sample(-1000,100,2000),sample(1000,100,2000,'peer')]));
    const flat=await range();
    assert.ok(flat.min>90&&flat.max<110,'A narrow positive visible band uses the plot height');
    await page.evaluate(()=>draw([sample(-10,0)]));assert.deepEqual(await range(),{min:0,max:1});
    await page.evaluate(()=>draw([]));assert.deepEqual(await range(),{min:0,max:1});
    assert.equal(await page.locator('svg path[d*="NaN"],svg path[d*="Infinity"]').count(),0);
    assert.deepEqual(errors,[]);
    report({ok:true,checks:['Visible-window Y range and boundary interpolation','Zoom, pan and preset rescale',
      'Both curves and concurrent peaks share the range','Flat, zero and empty ranges remain finite']});
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
