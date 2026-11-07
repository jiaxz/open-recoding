// Focused navigation palette: folders, Session cards, selection, unread and focus.
const assert=require('node:assert/strict'),path=require('node:path');
const {execFileSync}=require('node:child_process');
const {project,testDirectory,environment,startServer,launchBrowser,cleanup,report}=require('./support.cjs');

(async()=>{
  const temp=testDirectory('recoding-session-palette-'),workspace=path.join(temp,'workspace');
  const fixtures=JSON.parse(execFileSync(path.join(project,'.venv/bin/python3'),['-c',
    'import json,sys; from session import SessionStore; from pathlib import Path; root=Path(sys.argv[1]); specs=[(root,"推理与实现","grok-4.7"),(root,"图表分析","gemini-4-argon"),(root/"research","长名称 Session - 检查目录中的数据并保留详细结果","gemini-3.8-flash")]; print(json.dumps([SessionStore.create(p,n,{"model":m}).info() for p,n,m in specs]))',
    workspace],{cwd:project,env:environment(),encoding:'utf8'}));
  let server,browser,result;const errors=[];
  try{
    server=startServer(workspace);const base=await server.ready;browser=await launchBrowser();
    const page=await browser.newPage({viewport:{width:1450,height:950}});page.on('pageerror',error=>errors.push(error.message));
    await page.emulateMedia({reducedMotion:'reduce'});
    // Expose model labels for the palette fixture without opening history or starting workers.
    await page.route('**/api/sessions*',async route=>{
      if(route.request().method()!=='GET'||new URL(route.request().url()).pathname!=='/api/sessions')return route.continue();
      const response=await route.fetch(),data=await response.json();
      data.sessions=data.sessions.map(item=>({...item,model:fixtures.find(f=>f.id===item.id)?.model}));
      await route.fulfill({response,json:data});
    });
    await page.goto(base,{waitUntil:'domcontentloaded'});await page.waitForFunction(()=>workspaceSync.loaded);
    const row=id=>page.locator(`.directory-sessions > .session-row[data-id="${id}"]`);
    const style=async node=>node.evaluate(element=>{
      const row=getComputedStyle(element),title=getComputedStyle(element.querySelector('strong'));
      return {background:row.backgroundColor,border:row.borderLeftColor,shadow:row.boxShadow,
        color:title.color,weight:Number(title.fontWeight),height:element.getBoundingClientRect().height,
        glyph:getComputedStyle(element.querySelector('.session-glyph')).color};
    });
    const folder=await page.locator(`.workspace-group[data-directory="${workspace}"] > summary`).evaluate(node=>({background:getComputedStyle(node).backgroundColor,color:getComputedStyle(node).color,weight:Number(getComputedStyle(node).fontWeight)}));
    const normal=await style(row(fixtures[0].id));
    assert.notEqual(normal.background,folder.background);assert.notEqual(normal.color,folder.color);assert(folder.weight<normal.weight);
    const choose=async index=>page.evaluate(({items,index})=>{state.session={...items[index]};renderSessionNavigation(items);},{items:fixtures,index});
    const models=[];
    for(let i=0;i<fixtures.length;i++){
      await choose(i);const selected=await style(row(fixtures[i].id));models.push(selected);
      assert.notEqual(selected.background,normal.background);assert(selected.shadow.includes('inset'));
      assert(selected.weight>normal.weight);assert.equal(selected.height,normal.height);
      assert.equal(await row(fixtures[i].id).locator('.session-item').getAttribute('aria-current'),'page');
    }
    assert.equal(new Set(models.map(item=>item.background)).size,3,'Selection preserves all three model palettes');
    assert.equal(new Set(models.map(item=>item.glyph)).size,3);
    await choose(0);
    await row(fixtures[1].id).hover();assert.notEqual((await style(row(fixtures[1].id))).background,normal.background);
    await page.mouse.move(900,50);
    await row(fixtures[1].id).evaluate(node=>{node.classList.add('has-unread');node.querySelector('.session-status').textContent='2 新消息';});
    const unread=await style(row(fixtures[1].id));assert.notEqual(unread.background,normal.background);assert.notEqual(unread.background,models[0].background);
    await row(fixtures[0].id).evaluate(node=>{node.classList.add('has-unread');node.querySelector('.session-status').textContent='1 新消息';});
    assert.equal((await style(row(fixtures[0].id))).background,models[0].background,'Unread does not erase selected state');
    await row(fixtures[0].id).locator('.session-item').focus();await page.keyboard.press('Shift+Tab');await page.keyboard.press('Tab');
    assert.equal(await row(fixtures[0].id).evaluate(node=>getComputedStyle(node).outlineStyle),'solid');
    await page.setViewportSize({width:860,height:800});
    await page.evaluate(()=>document.documentElement.style.setProperty('--sidebar-width','220px'));
    assert(await page.locator('#session-list').evaluate(node=>node.scrollWidth<=node.clientWidth+1),'Compact tree must not scroll horizontally');
    const fitting=await row(fixtures[2].id).evaluate(node=>{
      const box=node.getBoundingClientRect(),title=node.querySelector('strong'),actions=node.querySelector('.session-actions').getBoundingClientRect();
      return title.getBoundingClientRect().width>20&&actions.right<=box.right+1&&getComputedStyle(title).textOverflow==='ellipsis';
    });assert(fitting,'Long titles and actions fit together');
    await page.locator('.sidebar').screenshot({path:path.join(temp,'sidebar-palette.png')});
    assert.deepEqual(errors,[]);
    result={passed:true,checks:['Folder/Session surfaces and title hierarchy are distinct',
      'Full-row selected state keeps model colors, title emphasis and a side marker without changing row height',
      'Hover, amber unread and keyboard focus remain distinguishable; selected unread retains selection',
      'Compact nested tree, long titles and actions fit without horizontal scrolling'],preview:'sidebar-palette.png',external_model_api_calls:0,full_suite_run:false,errors};
  }finally{await cleanup({browser,server});}
  report(result);
})().catch(error=>{console.error(error);process.exitCode=1;});
